"""ECL computation engine."""

# An auditor reproduces a result from these values, so they must describe what
# the code actually does. v1.1.0 adds the validate-time PD preview and corrects
# PD_METHOD, which previously claimed 299 powers and "Stage 1/3" normalization
# while pd_engine.compute_pd performs 300 powers and makes only Stage 3 absorbing.
ENGINE_VERSION = "v1.1.0"
ENGINE_RELEASED_DATE = "2026-06-01"
PD_METHOD = "Transition matrix with 300 monthly powers, Stage 3 absorbing"
LGD_METHOD = "Proportional collateral allocation with haircut and time discount"
EAD_METHOD = "IFRS 9 stage-based snapshot rundown with missed payment compounding"
