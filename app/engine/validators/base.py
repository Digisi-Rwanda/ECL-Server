"""Shared validation utilities for upload DataFrames."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


MAX_ISSUES = 100

ValidationLevel = Literal["warn", "block"]

STAGING_VALUES = frozenset({"Stage 1", "Stage 2", "Stage 3"})


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    level: ValidationLevel
    title: str
    location: str
    fix: str
    category: str | None = None


@dataclass
class ValidationResult:
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not any(issue.level == "block" for issue in self.issues)

    @property
    def has_warnings(self) -> bool:
        return any(issue.level == "warn" for issue in self.issues)

    def add(self, issue: ValidationIssue) -> None:
        if len(self.issues) >= MAX_ISSUES:
            return
        self.issues.append(issue)

    def add_block(
        self,
        *,
        title: str,
        location: str,
        fix: str,
        category: str | None = None,
    ) -> None:
        self.add(
            ValidationIssue(
                level="block", title=title, location=location, fix=fix, category=category
            )
        )

    def add_warn(
        self,
        *,
        title: str,
        location: str,
        fix: str,
        category: str | None = None,
    ) -> None:
        self.add(
            ValidationIssue(
                level="warn", title=title, location=location, fix=fix, category=category
            )
        )

    @property
    def issue_cap_reached(self) -> bool:
        return len(self.issues) >= MAX_ISSUES

    def remaining_capacity(self) -> int:
        return max(0, MAX_ISSUES - len(self.issues))
