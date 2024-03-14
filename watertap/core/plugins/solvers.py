#################################################################################
# WaterTAP Copyright (c) 2020-2023, The Regents of the University of California,
# through Lawrence Berkeley National Laboratory, Oak Ridge National Laboratory,
# National Renewable Energy Laboratory, and National Energy Technology
# Laboratory (subject to receipt of any required approvals from the U.S. Dept.
# of Energy). All rights reserved.
#
# Please see the files COPYRIGHT.md and LICENSE.md for full copyright and license
# information, respectively. These files are also available online at the URL
# "https://github.com/watertap-org/watertap/"
#################################################################################

import logging

import pyomo.environ as pyo
from pyomo.common.collections import Bunch
from pyomo.core.base.block import _BlockData
from pyomo.core.kernel.block import IBlock
from pyomo.solvers.plugins.solvers.IPOPT import IPOPT
from pyomo.common.dependencies import attempt_import

import idaes.core.util.scaling as iscale
from idaes.core.util.scaling import (
    get_scaling_factor,
    set_scaling_factor,
    unset_scaling_factor,
)
from idaes.logger import getLogger

IPython, IPython_available = attempt_import("IPython")

_log = getLogger("watertap.core")

_pyomo_nl_writer_log = logging.getLogger("pyomo.repn.plugins.nl_writer")


def _pyomo_nl_writer_logger_filter(record):
    msg = record.getMessage()
    if "scaling_factor" in msg and "model contains export suffix" in msg:
        return False
    return True


@pyo.SolverFactory.register(
    "ipopt-watertap",
    doc="The Ipopt NLP solver, with user-based variable and automatic Jacobian constraint scaling",
)
class IpoptWaterTAP(IPOPT):
    def __init__(self, **kwds):
        kwds["name"] = "ipopt-watertap"
        self._cleanup_needed = False
        super().__init__(**kwds)

    def _presolve(self, *args, **kwds):
        if len(args) > 1 or len(args) == 0:
            raise TypeError(
                f"IpoptWaterTAP.solve takes 1 positional argument but {len(args)} were given"
            )
        if not isinstance(args[0], (_BlockData, IBlock)):
            raise TypeError(
                "IpoptWaterTAP.solve takes 1 positional argument: a Pyomo ConcreteModel or Block"
            )

        # until proven otherwise
        self._cleanup_needed = False

        self._tee = kwds.get("tee", False)

        # Set the default watertap options
        if "tol" not in self.options:
            self.options["tol"] = 1e-08
        if "constr_viol_tol" not in self.options:
            self.options["constr_viol_tol"] = 1e-08
        if "acceptable_constr_viol_tol" not in self.options:
            self.options["acceptable_constr_viol_tol"] = 1e-08
        if "ma27_pivtol" not in self.options:
            self.options["ma27_pivtol"] = 1e-02
        if "bound_relax_factor" not in self.options:
            self.options["bound_relax_factor"] = 0.0
        if "honor_original_bounds" not in self.options:
            self.options["honor_original_bounds"] = "no"

        if not self._is_user_scaling():
            super()._presolve(*args, **kwds)
            self._cleanup()
            return

        if self._tee:
            print(
                "ipopt-watertap: Ipopt with user variable scaling and IDAES jacobian constraint scaling"
            )

        # These options are typically available with gradient-scaling, and they
        # have corresponding options in the IDAES constraint_autoscale_large_jac
        # function. Here we use their Ipopt names and default values, see
        # https://coin-or.github.io/Ipopt/OPTIONS.html#OPT_NLP_Scaling
        max_grad = self._get_option("nlp_scaling_max_gradient", 100)
        min_scale = self._get_option("nlp_scaling_min_value", 1e-08)

        # These options are custom for the IDAES constraint_autoscale_large_jac
        # function. We expose them as solver options as this has become part
        # of the solve process.
        ignore_variable_scaling = self._get_option("ignore_variable_scaling", False)
        ignore_constraint_scaling = self._get_option("ignore_constraint_scaling", False)

        self._model = args[0]
        self._cache_scaling_factors()
        self._cleanup_needed = True
        _pyomo_nl_writer_log.addFilter(_pyomo_nl_writer_logger_filter)

        # NOTE: This function sets the scaling factors on the
        #       constraints. Hence we cache the constraint scaling
        #       factors and reset them to their original values
        #       so that repeated calls to solve change the scaling
        #       each time based on the initial values, just like in Ipopt.
        try:
            _, _, nlp = iscale.constraint_autoscale_large_jac(
                self._model,
                ignore_constraint_scaling=ignore_constraint_scaling,
                ignore_variable_scaling=ignore_variable_scaling,
                max_grad=max_grad,
                min_scale=min_scale,
            )
        except Exception as err:
            nlp = None
            if str(err) == "Error in AMPL evaluation":
                print(
                    "ipopt-watertap: Issue in AMPL function evaluation; Jacobian constraint scaling not applied."
                )
                halt_on_ampl_error = self.options.get("halt_on_ampl_error", "yes")
                if halt_on_ampl_error == "no":
                    print(
                        "ipopt-watertap: halt_on_ampl_error=no, so continuing with optimization."
                    )
                else:
                    self._cleanup()
                    raise RuntimeError(
                        "Error in AMPL evaluation.\n"
                        "Run ipopt with halt_on_ampl_error=yes and symbolic_solver_labels=True to see the affected function."
                    )
            else:
                print("Error in constraint_autoscale_large_jac")
                self._cleanup()
                raise

        # set different default for `alpha_for_y` if this is an LP
        # see: https://coin-or.github.io/Ipopt/OPTIONS.html#OPT_alpha_for_y
        if nlp is not None:
            if nlp.nnz_hessian_lag() == 0:
                if "alpha_for_y" not in self.options:
                    self.options["alpha_for_y"] = "bound-mult"

        try:
            # this creates the NL file, among other things
            return super()._presolve(*args, **kwds)
        except:
            self._cleanup()
            raise

    def _cleanup(self):
        if self._cleanup_needed:
            self._reset_scaling_factors()
            # remove our reference to the model
            del self._model
            _pyomo_nl_writer_log.removeFilter(_pyomo_nl_writer_logger_filter)

    def _postsolve(self):
        self._cleanup()
        return super()._postsolve()

    def _cache_scaling_factors(self):
        self._scaling_cache = [
            (c, get_scaling_factor(c))
            for c in self._model.component_data_objects(
                pyo.Constraint, active=True, descend_into=True
            )
        ]

    def _reset_scaling_factors(self):
        for c, s in self._scaling_cache:
            if s is None:
                unset_scaling_factor(c)
            else:
                set_scaling_factor(c, s)
        del self._scaling_cache

    def _get_option(self, option_name, default_value):
        # NOTE: options get reset to their original value at the end of the
        #       OptSolver.solve. The options in _presolve (where this is called)
        #       are already copies of the original, so it is safe to pop them so
        #       they don't get sent to Ipopt.
        option_value = self.options.pop(option_name, None)
        if option_value is None:
            option_value = default_value
        else:
            if self._tee:
                print(f"ipopt-watertap: {option_name}={option_value}")
        return option_value

    def _is_user_scaling(self):
        if "nlp_scaling_method" not in self.options:
            self.options["nlp_scaling_method"] = "user-scaling"
        if self.options["nlp_scaling_method"] != "user-scaling":
            if self._tee:
                print(
                    "The ipopt-watertap solver is designed to be run with user-scaling. "
                    f"Ipopt with nlp_scaling_method={self.options['nlp_scaling_method']} will be used instead"
                )
            return False
        return True


class _BaseDebugSolverWrapper:

    # defined by the derived class,
    # created on the fly
    _base_solver = None
    _debug_solver_name = None

    def __init__(self, **kwds):

        kwds["name"] = self._debug_solver_name
        self.options = Bunch()
        if kwds.get("options") is not None:
            for key in kwds["options"]:
                setattr(self.options, key, kwds["options"][key])

        self._value_cache = pyo.ComponentMap()

    def restore_initial_values(self, blk):
        for var in blk.component_data_objects(pyo.Var, descend_into=True):
            var.set_value(self._value_cache[var], skip_validation=True)

    def _cache_initial_values(self, blk):
        for v in blk.component_data_objects(pyo.Var, descend_into=True):
            self._value_cache[v] = v.value

    def solve(self, blk, *args, **kwds):

        if not IPython_available:
            raise ImportError(f"The DebugSolverWrapper requires ipython.")

        solver = pyo.SolverFactory(self._base_solver)

        for k, v in self.options.items():
            solver.options[k] = v

        self._cache_initial_values(blk)

        try:
            results = solver.solve(blk, *args, **kwds)
        except:
            results = None
        if results is not None and pyo.check_optimal_termination(results):
            return results

        # prevent circular imports
        from watertap.core.util import model_debug_mode

        # deactivate the model debug mode so we don't
        # nest this environment within itself
        model_debug_mode.deactivate()

        self.restore_initial_values(blk)
        debug = self

        # else there was a problem
        print(f"Solver debugging mode: the block {blk.name} failed to solve.")
        print(f"{blk.name} is called `blk` in this context.")
        print(f"The solver {solver.name} is available in the variable `solver`.")
        print(f"The Initial values have be restored into the block.")
        print(
            f"You can restore them anytime by calling `debug.restore_initial_values(blk)`."
        )
        print(
            f"The model has been loaded into an IDAES DiagnosticsToolbox instance called `dt`."
        )
        from idaes.core.util.model_diagnostics import DiagnosticsToolbox

        dt = DiagnosticsToolbox(blk)
        # dt.report_structural_issues()
        IPython.embed(colors="neutral")

        # activate the model debug mode
        # to keep the state the same
        model_debug_mode.activate()

        return results


def create_debug_solver_wrapper(solver_name):

    assert pyo.SolverFactory(solver_name).available()

    debug_solver_name = f"debug-solver-wrapper-{solver_name}"

    @pyo.SolverFactory.register(
        debug_solver_name,
        doc=f"Debug solver wrapper for {solver_name}",
    )
    class DebugSolverWrapper(_BaseDebugSolverWrapper):
        _base_solver = solver_name
        _debug_solver_name = debug_solver_name

    return debug_solver_name


## reconfigure IDAES to use the ipopt-watertap solver
import idaes

_default_solver_config_value = idaes.cfg.get("default_solver")
_idaes_default_solver = _default_solver_config_value._default

_default_solver_config_value.set_default_value("ipopt-watertap")
if not _default_solver_config_value._userSet:
    _default_solver_config_value.reset()
