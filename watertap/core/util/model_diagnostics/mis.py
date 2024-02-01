"""
Minimal Intractable System (MIS) finder

See: https://www.sce.carleton.ca/faculty/chinneck/docs/CPAIOR07InfeasibilityTutorial.pdf

"""
import pyomo.environ as pyo

from pyomo.core.plugins.transform.add_slack_vars import AddSlackVariables

from pyomo.core.plugins.transform.hierarchy import IsomorphicTransformation

from pyomo.common.modeling import unique_component_name
from pyomo.common.collections import ComponentMap, ComponentSet

from pyomo.opt import WriterFactory

from idaes.core.solvers import get_solver

_default_nl_writer = WriterFactory.get_class("nl")


class _VariableBoundsAsConstraints(IsomorphicTransformation):
    """Replace all variables bounds and domain information with constraints.

    Leaves fixed Vars untouched (for now)
    """

    def _apply_to(self, instance, **kwds):

        boundconstrblockname = unique_component_name(instance, "_variable_bounds")
        instance.add_component(boundconstrblockname, pyo.Block())
        boundconstrblock = instance.component(boundconstrblockname)

        for v in instance.component_data_objects(pyo.Var, descend_into=True):
            if v.fixed:
                continue
            lb, ub = v.bounds
            if lb is None and ub is None:
                continue
            var_name = v.getname(fully_qualified=True)
            if lb is not None:
                con_name = "lb_for_" + var_name
                con = pyo.Constraint(expr=(lb, v, None))
                boundconstrblock.add_component(con_name, con)
            if ub is not None:
                con_name = "ub_for_" + var_name
                con = pyo.Constraint(expr=(None, v, ub))
                boundconstrblock.add_component(con_name, con)

            # now we deactivate the variable bounds / domain
            v.domain = pyo.Reals
            v.setlb(None)
            v.setub(None)


def compute_infeasibility_explanation(model, solver=None, tee=False, tolerance=1e-8):
    """
    This function attempts to determine why a given model is infeasible. It deploys
    two main algorithms:

    1. Successfully relaxes the constraints of the problem, and reports to the user
       some sets of constraints and variable bounds, which when relaxed, creates a
       feasible model.
    2. Uses the information collected from (1) to attempt to compute a Minimal
       Infeasible System (MIS), which is a set of constraints and variable bounds
       which appear to be in conflict with each other. It is minimal in the sense
       that removing any single constraint or variable bound would result in a
       feasible subsystem.

    Args
    ----
        model: A pyomo block
        solver (optional): A pyomo solver, a string, or None
        tee (optional):  Display intermediate solves conducted (False)
        tolerance (optional): The feasibility tolerance to use when declaring a
            constraint feasible (1e-08)
    """

    # hold the original harmless
    modified_model = model.clone()

    if solver is None:
        solver = get_solver()
    elif isinstance(solver, str):
        solver = pyo.SolverFactory(solver)
    else:
        # assume we have a solver
        assert solver.available()

    # first, cache the values we get
    _value_cache = ComponentMap()
    for v in model.component_data_objects(pyo.Var, descend_into=True):
        _value_cache[v] = v.value

    # finding proper reference
    if model.parent_block() is None:
        common_name = ""
    else:
        common_name = model.name + "."

    _modified_model_var_to_original_model_var = ComponentMap()
    _modified_model_value_cache = ComponentMap()

    for v in model.component_data_objects(pyo.Var, descend_into=True):
        modified_model_var = modified_model.find_component(v.name[len(common_name) :])

        _modified_model_var_to_original_model_var[modified_model_var] = v
        _modified_model_value_cache[modified_model_var] = _value_cache[v]
        modified_model_var.set_value(_value_cache[v], skip_validation=True)

    # TODO: For WT / IDAES models, we should probably be more
    #       selective in *what* we elasticize. E.g., it probably
    #       does not make sense to elasticize property calculations
    #       and maybe certain other equality constraints calculating
    #       values. Maybe we shouldn't elasticize *any* equality
    #       constraints.
    #       For example, elasticizing the calculation of mass fraction
    #       makes absolutely no sense and will just be noise for the
    #       modeler to sift through. We could try to sort the constraints
    #       such that we look for those with linear coefficients `1` on
    #       some term and leave those be.
    # move the variable bounds to the constraints
    _VariableBoundsAsConstraints().apply_to(modified_model)

    AddSlackVariables().apply_to(modified_model)
    slack_block = modified_model._core_add_slack_variables

    for v in slack_block.component_data_objects(pyo.Var):
        v.fix(0)
    # start with variable bounds -- these are the easist to interpret
    for c in modified_model._variable_bounds.component_data_objects(
        pyo.Constraint, descend_into=True
    ):
        plus = slack_block.component(f"_slack_plus_{c.name}")
        minus = slack_block.component(f"_slack_minus_{c.name}")
        assert not (plus is None and minus is None)
        if plus is not None:
            plus.unfix()
        if minus is not None:
            minus.unfix()

    # TODO: Elasticizing too much at once seems to cause Ipopt trouble.
    #       After an initial sweep, we should just fix one elastic variable
    #       and put everything else on a stack of "constraints to elasticize".
    #       We elastisize one constraint at a time and fix one constraint at a time.
    #       After fixing an elastic variable, we elasticize a single constraint it
    #       appears in and put the remaining constraints on the stack. If the resulting problem
    #       is feasible, we keep going "down the tree". If the resulting problem is
    #       infeasible or cannot be solved, we elasticize a single constraint from
    #       the top of the stack.
    #       The algorithm stops when the stack is empty and the subproblem is infeasible.
    #       Along the way, any time the current problem is infeasible we can check to
    #       see if the current set of constraints in the filter is as a collection of
    #       infeasible constraints -- to terminate early.
    #       However, while more stable, this is much more computationally intensive.
    #       So, we leave the implementation simpler for now and consider this as
    #       a potential extension if this tool sometimes cannot report a good answer.
    # Phase 1 -- build the initial set of constraints, or prove feasibility
    msg = ""
    fixed_slacks = ComponentSet()
    elastic_filter = ComponentSet()

    def _constraint_loop(relaxed_things, msg):
        if msg == "":
            msg += f"Model {model.name} may be infeasible. A feasible solution was found with only the following {relaxed_things} relaxed:\n"
        else:
            msg += f"Another feasible solution was found with only the following {relaxed_things} relaxed:\n"
        while True:

            def _constraint_generator():
                elastic_filter_size_initial = len(elastic_filter)
                for v in slack_block.component_data_objects(pyo.Var):
                    if v.value > tolerance:
                        constr = _get_constraint(modified_model, v)
                        yield constr, v.value
                        v.fix(0)
                        fixed_slacks.add(v)
                        elastic_filter.add(constr)
                if len(elastic_filter) == elastic_filter_size_initial:
                    raise Exception(f"Found model {model.name} to be feasible!")

            msg = _get_results_with_value(_constraint_generator(), msg)
            for var, val in _modified_model_value_cache.items():
                var.set_value(val, skip_validation=True)
            results = solver.solve(modified_model, tee=tee)
            if pyo.check_optimal_termination(results):
                msg += f"Another feasible solution was found with only the following {relaxed_things} relaxed:\n"
            else:
                break
        return msg

    results = solver.solve(modified_model, tee=tee)
    if pyo.check_optimal_termination(results):
        msg = _constraint_loop("variable bounds", msg)

    # next, try relaxing the inequality constraints
    for v in slack_block.component_data_objects(pyo.Var):
        c = _get_constraint(modified_model, v)
        if c.equality:
            # equality constraint
            continue
        if v not in fixed_slacks:
            v.unfix()

    results = solver.solve(modified_model, tee=tee)
    if pyo.check_optimal_termination(results):
        msg = _constraint_loop("inequality constraints and/or variable bounds", msg)

    for v in slack_block.component_data_objects(pyo.Var):
        if v not in fixed_slacks:
            v.unfix()

    results = solver.solve(modified_model, tee=tee)
    if pyo.check_optimal_termination(results):
        msg = _constraint_loop(
            "inequality constraints, equality constraints, and/or variable bounds", msg
        )

    if len(elastic_filter) == 0:
        # load the feasible solution into the original model
        for modified_model_var, v in _modified_model_var_to_original_model_var.items():
            v.set_value(modified_model_var.value, skip_validation=True)
        results = solver.solve(model, tee=tee)
        if pyo.check_optimal_termination(results):
            print(f"A feasible solution was found!")
        else:
            print(
                f"Could not find a feasible solution with violated constraints or bounds. This model is likely unstable"
            )

    # Phase 2 -- deletion filter
    # TODO: the model created here seems to mess with the nl_v2
    #       writer sometimes. So we temporarily switch to nl_v1 writer.
    WriterFactory.register("nl")(WriterFactory.get_class("nl_v1"))

    # remove slacks by fixing them to 0
    for v in slack_block.component_data_objects(pyo.Var):
        v.fix(0)
    for o in modified_model.component_data_objects(pyo.Objective, descend_into=True):
        o.deactivate()

    # mark all constraints not in the filter as inactive
    for c in modified_model.component_data_objects(pyo.Constraint):
        if c in elastic_filter:
            continue
        else:
            c.deactivate()

    try:
        results = solver.solve(modified_model, tee=tee)
    except:
        results = None

    if pyo.check_optimal_termination(results):
        msg += "Could not determine Minimal Intractable System\n"
    else:
        deletion_filter = []
        guards = []
        for constr in elastic_filter:
            constr.deactivate()
            for var, val in _modified_model_value_cache.items():
                var.set_value(val, skip_validation=True)
            try:
                results = solver.solve(modified_model, tee=tee)
            except:
                math_failure = True
            else:
                math_failure = False

            if math_failure:
                constr.activate()
                guards.append(constr)
            elif pyo.check_optimal_termination(results):
                constr.activate()
                deletion_filter.append(constr)
            else:  # still infeasible without this constraint
                pass

        msg += "Computed Minimal Intractable System (MIS)!\n"
        msg += "Constraints / bounds in MIS:\n"
        msg = _get_results(deletion_filter, msg)
        msg += "Constraints / bounds in guards for stability:"
        msg = _get_results(guards, msg)

    WriterFactory.register("nl")(_default_nl_writer)

    print(msg)


def _get_results_with_value(constr_value_generator, msg=None):
    if msg is None:
        msg = ""
    for c, value in constr_value_generator:
        c_name = c.name
        if "_variable_bounds" in c_name:
            name = c.local_name
            if "lb" in name:
                msg += f"\tlb of var {name[7:]} by {value}\n"
            elif "ub" in name:
                msg += f"\tub of var {name[7:]} by {value}\n"
            else:
                raise RuntimeError("unrecongized var name")
        else:
            msg += f"\tconstraint: {c_name} by {value}\n"
    return msg


def _get_results(constr_generator, msg=None):
    if msg is None:
        msg = ""
    for c in constr_generator:
        c_name = c.name
        if "_variable_bounds" in c_name:
            name = c.local_name
            if "lb" in name:
                msg += f"\tlb of var {name[7:]}\n"
            elif "ub" in name:
                msg += f"\tub of var {name[7:]}\n"
            else:
                raise RuntimeError("unrecongized var name")
        else:
            msg += f"\tconstraint: {c_name}\n"
    return msg


def _get_constraint(modified_model, v):
    if "_slack_plus_" in v.name:
        constr = modified_model.find_component(v.local_name[len("_slack_plus_") :])
        if constr is None:
            raise RuntimeError(
                "Bad constraint name {v.local_name[len('_slack_plus_'):]}"
            )
        return constr
    elif "_slack_minus_" in v.name:
        constr = modified_model.find_component(v.local_name[len("_slack_minus_") :])
        if constr is None:
            raise RuntimeError(
                "Bad constraint name {v.local_name[len('_slack_minus_'):]}"
            )
        return constr
    else:
        raise RuntimeError("Bad var name {v.name}")
