"""ECL computation engine."""

# An auditor reproduces a result from these values, so they must describe what
# the code actually does. v1.1.0 adds the validate-time PD preview and corrects
# PD_METHOD, which previously claimed 299 powers and "Stage 1/3" normalization
# while pd_engine.compute_pd performs 300 powers and makes only Stage 3 absorbing.
# v1.2.0 implements the "Lifetime PD Term Structure" spec: 299 powers, Stage 1's
# 1 -> 3 share folded into Stage 2, and marginal PD 0 once S3 has saturated.
# v1.3.0 implements "ECL Calculation Specification" v1.0 for LGD, EAD and ECL
# (input rules, Stage 3 PD = 100%, missed payments, cap, reconciliation).
ENGINE_VERSION = "v1.3.0"
ENGINE_RELEASED_DATE = "2026-10-29"
PD_METHOD = (
    "Transition matrix with 299 monthly powers, Stage 1 -> 3 folded into Stage 2, "
    "Stage 3 absorbing"
)
LGD_METHOD = (
    "Customer collateral split by exposure share, (1 - haircut) x (1 + EIR)^-years; "
    "LGD = max(0, 1 - collateral / EAD) x (1 - cure rate)"
)
EAD_METHOD = (
    "Stage 1 12 months, Stage 2 to maturity, Stage 3/matured/zero-rate single row; "
    "level instalment; EAD = balance compounded over up to 3 missed payments"
)
