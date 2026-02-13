"""
Validation Engine - checks signal quality before activation.

This module implements comprehensive validation logic to ensure signals meet
quality standards before they are activated and sent to users. It performs
sanity checks on price levels, risk-reward ratios, latency, and duplicate
detection.

Validation checks:
1. Price sanity: Entry < TP1 < TP2 < TP3 (for LONG), reversed for SHORT
2. RR ratio: Must be >= 0.5, warns if < 1.0
3. Risk distance: SL can't be absurdly far from entry
4. Latency: Signal must be recent (configurable)
5. Price precision: Reasonable decimal places for asset class
6. Duplicate detection: Same symbol/direction/entry already exists
"""

from datetime import datetime, timedelta
from typing import Tuple, List, Optional, Dict, Any
from dataclasses import dataclass

from app.models.canonical_signal import (
    CanonicalSignal,
    SignalDirection,
    AssetClass,
    ValidationResult,
)


@dataclass
class ValidationConfig:
    """Configuration for the validation engine."""

    max_latency_seconds: int = 300  # 5 minutes
    warn_latency_seconds: int = 120  # 2 minutes
    min_rr_ratio: float = 0.5  # Minimum acceptable R/R
    warn_rr_ratio: float = 1.0  # Warn if below this
    max_risk_pct: Dict[AssetClass, float] = None  # Overridable per asset class

    def __post_init__(self):
        """Initialize default max_risk_pct if not provided."""
        if self.max_risk_pct is None:
            self.max_risk_pct = {
                AssetClass.FUTURES: 0.03,  # 3% of entry price
                AssetClass.FOREX: 0.02,  # 2% of entry price
                AssetClass.CRYPTO: 0.15,  # 15% of entry price
                AssetClass.STOCKS: 0.05,  # 5% of entry price
                AssetClass.OTHER: 0.10,  # 10% of entry price
            }


class ValidationEngine:
    """
    Validates signal quality before activation.

    All validation methods are static and pure functions (no side effects).
    The engine computes confidence scores based on validation results.
    """

    # Default configuration
    DEFAULT_CONFIG = ValidationConfig()

    @staticmethod
    def validate(
        signal: CanonicalSignal,
        config: Optional[ValidationConfig] = None,
        current_time: Optional[datetime] = None,
    ) -> ValidationResult:
        """
        Run all validation checks on a signal.

        Performs comprehensive validation across price sanity, risk metrics,
        latency, and precision. Returns detailed results with errors, warnings,
        and calculated confidence score.

        Args:
            signal: The CanonicalSignal to validate
            config: ValidationConfig with thresholds (uses default if None)
            current_time: Current UTC time for latency check (uses utcnow if None)

        Returns:
            ValidationResult with is_valid, errors, warnings, and confidence_score
        """
        if config is None:
            config = ValidationEngine.DEFAULT_CONFIG

        if current_time is None:
            current_time = datetime.utcnow()

        errors: List[str] = []
        warnings: List[str] = []

        # Run all validation checks
        price_errors, price_warnings = ValidationEngine._check_price_sanity(signal)
        errors.extend(price_errors)
        warnings.extend(price_warnings)

        rr_errors, rr_warnings = ValidationEngine._check_rr_ratio(signal, config)
        errors.extend(rr_errors)
        warnings.extend(rr_warnings)

        risk_errors, risk_warnings = ValidationEngine._check_risk_distance(signal, config)
        errors.extend(risk_errors)
        warnings.extend(risk_warnings)

        latency_errors, latency_warnings = ValidationEngine._check_latency(
            signal, config, current_time
        )
        errors.extend(latency_errors)
        warnings.extend(latency_warnings)

        precision_errors, precision_warnings = ValidationEngine._check_price_precision(signal)
        errors.extend(precision_errors)
        warnings.extend(precision_warnings)

        # Calculate confidence score
        confidence_score = ValidationEngine._calculate_confidence(errors, warnings)

        # Signal is valid if no errors (warnings don't invalidate)
        is_valid = len(errors) == 0

        return ValidationResult(
            is_valid=is_valid,
            errors=errors,
            warnings=warnings,
            rr_ratio=signal.rr_ratio,
            risk_distance=signal.risk_distance,
            confidence_score=confidence_score,
        )

    @staticmethod
    def _check_price_sanity(signal: CanonicalSignal) -> Tuple[List[str], List[str]]:
        """
        Validate that price levels are in correct order.

        For LONG: entry < tp1 < tp2 (if exists) < tp3 (if exists), and sl < entry
        For SHORT: entry > tp1 > tp2 (if exists) > tp3 (if exists), and sl > entry

        Returns:
            Tuple of (errors, warnings)
        """
        errors: List[str] = []
        warnings: List[str] = []

        if signal.direction == SignalDirection.LONG:
            # For LONG: entry must be above SL
            if signal.entry_price <= signal.sl:
                errors.append(
                    f"LONG entry ({signal.entry_price}) must be above SL ({signal.sl})"
                )

            # TP1 must be above entry
            if signal.tp1 <= signal.entry_price:
                errors.append(
                    f"TP1 ({signal.tp1}) must be above entry ({signal.entry_price})"
                )

            # TP2 must be above TP1 (if exists)
            if signal.tp2 is not None and signal.tp2 <= signal.tp1:
                errors.append(f"TP2 ({signal.tp2}) must be above TP1 ({signal.tp1})")

            # TP3 must be above TP2 (if exists)
            if (signal.tp3 is not None and signal.tp2 is not None and
                signal.tp3 <= signal.tp2):
                errors.append(f"TP3 ({signal.tp3}) must be above TP2 ({signal.tp2})")

        else:  # SHORT
            # For SHORT: entry must be below SL
            if signal.entry_price >= signal.sl:
                errors.append(
                    f"SHORT entry ({signal.entry_price}) must be below SL ({signal.sl})"
                )

            # TP1 must be below entry
            if signal.tp1 >= signal.entry_price:
                errors.append(
                    f"TP1 ({signal.tp1}) must be below entry ({signal.entry_price})"
                )

            # TP2 must be below TP1 (if exists)
            if signal.tp2 is not None and signal.tp2 >= signal.tp1:
                errors.append(f"TP2 ({signal.tp2}) must be below TP1 ({signal.tp1})")

            # TP3 must be below TP2 (if exists)
            if (signal.tp3 is not None and signal.tp2 is not None and
                signal.tp3 >= signal.tp2):
                errors.append(f"TP3 ({signal.tp3}) must be below TP2 ({signal.tp2})")

        return errors, warnings

    @staticmethod
    def _check_rr_ratio(signal: CanonicalSignal, config: ValidationConfig) -> Tuple[List[str], List[str]]:
        """
        Validate risk-to-reward ratio.

        Args:
            signal: The signal to check
            config: Configuration with min_rr_ratio and warn_rr_ratio

        Returns:
            Tuple of (errors, warnings)
        """
        errors: List[str] = []
        warnings: List[str] = []

        if signal.rr_ratio is None:
            errors.append("RR ratio not calculated")
            return errors, warnings

        # Check minimum RR ratio
        if signal.rr_ratio < config.min_rr_ratio:
            errors.append(
                f"RR ratio ({signal.rr_ratio:.2f}) below minimum ({config.min_rr_ratio})"
            )

        # Warn if below preferred RR ratio
        if signal.rr_ratio < config.warn_rr_ratio:
            warnings.append(
                f"RR ratio ({signal.rr_ratio:.2f}) below preferred level ({config.warn_rr_ratio})"
            )

        # Warn if unusually high (possible data entry error)
        if signal.rr_ratio > 10.0:
            warnings.append(
                f"Unusually high RR ratio ({signal.rr_ratio:.2f}) - check for data entry errors"
            )

        return errors, warnings

    @staticmethod
    def _check_risk_distance(signal: CanonicalSignal, config: ValidationConfig) -> Tuple[List[str], List[str]]:
        """
        Validate that SL isn't absurdly far from entry.

        Checks that the risk distance (entry to SL) doesn't exceed configured
        percentage limits for the asset class.

        Args:
            signal: The signal to check
            config: Configuration with max_risk_pct per asset class

        Returns:
            Tuple of (errors, warnings)
        """
        errors: List[str] = []
        warnings: List[str] = []

        if signal.risk_distance is None or signal.entry_price == 0:
            errors.append("Risk distance not calculated or entry price is zero")
            return errors, warnings

        # Calculate risk as percentage of entry
        risk_pct = signal.risk_distance / signal.entry_price

        max_allowed_pct = config.max_risk_pct.get(
            signal.asset_class, config.max_risk_pct[AssetClass.OTHER]
        )

        if risk_pct > max_allowed_pct:
            errors.append(
                f"Risk distance ({risk_pct:.2%}) exceeds maximum for "
                f"{signal.asset_class.value} ({max_allowed_pct:.2%})"
            )

        # Warn if very tight SL (less than 0.1%)
        if risk_pct < 0.001:
            warnings.append(
                f"Very tight stop-loss ({risk_pct:.2%}) may trigger on noise"
            )

        return errors, warnings

    @staticmethod
    def _check_latency(
        signal: CanonicalSignal,
        config: ValidationConfig,
        current_time: datetime,
    ) -> Tuple[List[str], List[str]]:
        """
        Validate signal freshness.

        Signals that are too old are errors; signals that are moderately old
        generate warnings.

        Args:
            signal: The signal to check
            config: Configuration with max/warn latency in seconds
            current_time: Current UTC time for comparison

        Returns:
            Tuple of (errors, warnings)
        """
        errors: List[str] = []
        warnings: List[str] = []

        # Calculate age in seconds
        age_seconds = (current_time - signal.entry_time).total_seconds()

        # Check max latency
        if age_seconds > config.max_latency_seconds:
            errors.append(
                f"Signal too old ({age_seconds:.0f}s > {config.max_latency_seconds}s)"
            )

        # Warn if moderately old
        elif age_seconds > config.warn_latency_seconds:
            warnings.append(
                f"Signal age {age_seconds:.0f}s exceeds preferred freshness "
                f"({config.warn_latency_seconds}s)"
            )

        return errors, warnings

    @staticmethod
    def _check_price_precision(signal: CanonicalSignal) -> Tuple[List[str], List[str]]:
        """
        Validate reasonable decimal places for the asset class.

        Different asset classes have different conventions:
        - Futures: 0-2 decimals
        - Forex: 2-5 decimals
        - Crypto: 2-8 decimals
        - Stocks: 2 decimals

        Args:
            signal: The signal to check

        Returns:
            Tuple of (errors, warnings)
        """
        errors: List[str] = []
        warnings: List[str] = []

        # Extract decimal places from a price
        def get_decimal_places(price: float) -> int:
            """Count decimal places in a price."""
            str_price = f"{price:.10f}".rstrip("0")
            if "." in str_price:
                return len(str_price.split(".")[1])
            return 0

        prices_to_check = [
            ("entry", signal.entry_price),
            ("SL", signal.sl),
            ("TP1", signal.tp1),
        ]

        if signal.tp2 is not None:
            prices_to_check.append(("TP2", signal.tp2))

        if signal.tp3 is not None:
            prices_to_check.append(("TP3", signal.tp3))

        # Define precision limits by asset class
        precision_limits = {
            AssetClass.FUTURES: (0, 2),  # 0-2 decimals
            AssetClass.FOREX: (2, 5),  # 2-5 decimals
            AssetClass.CRYPTO: (2, 8),  # 2-8 decimals
            AssetClass.STOCKS: (2, 2),  # 2 decimals
            AssetClass.OTHER: (1, 5),  # 1-5 decimals
        }

        min_decimals, max_decimals = precision_limits.get(
            signal.asset_class, precision_limits[AssetClass.OTHER]
        )

        for price_name, price in prices_to_check:
            decimals = get_decimal_places(price)

            if decimals > max_decimals:
                warnings.append(
                    f"{price_name} has {decimals} decimals (max {max_decimals} "
                    f"for {signal.asset_class.value})"
                )

        return errors, warnings

    @staticmethod
    def _calculate_confidence(errors: List[str], warnings: List[str]) -> int:
        """
        Calculate confidence score 0-100 based on validation results.

        Scoring:
        - Start at 100
        - Each error: -15 points
        - Each warning: -5 points
        - Minimum score: 0

        Args:
            errors: List of error messages
            warnings: List of warning messages

        Returns:
            Confidence score from 0-100
        """
        score = 100
        score -= len(errors) * 15
        score -= len(warnings) * 5
        return max(0, score)

    @staticmethod
    def check_duplicate(
        signal: CanonicalSignal,
        recent_signals: List[CanonicalSignal],
        hours_back: float = 1.0,
    ) -> Tuple[bool, Optional[str]]:
        """
        Check if a similar signal already exists in recent history.

        Considers signals as duplicates if they have the same:
        - Symbol
        - Direction
        - Entry price (within 0.1%)

        Args:
            signal: The signal to check
            recent_signals: List of recent signals to check against
            hours_back: How far back to look (for reference/logging)

        Returns:
            Tuple of (is_duplicate, duplicate_signal_id)
        """
        entry_tolerance = signal.entry_price * 0.001  # 0.1% tolerance

        for existing in recent_signals:
            if (existing.symbol == signal.symbol and
                existing.direction == signal.direction and
                abs(existing.entry_price - signal.entry_price) <= entry_tolerance):
                return True, existing.id

        return False, None

    @staticmethod
    def validate_batch(
        signals: List[CanonicalSignal],
        config: Optional[ValidationConfig] = None,
        current_time: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        Validate multiple signals and return summary statistics.

        Args:
            signals: List of signals to validate
            config: Validation configuration (uses default if None)
            current_time: Current UTC time (uses utcnow if None)

        Returns:
            Dictionary with validation summary
        """
        if config is None:
            config = ValidationEngine.DEFAULT_CONFIG

        if current_time is None:
            current_time = datetime.utcnow()

        results = {
            "total": len(signals),
            "valid": 0,
            "invalid": 0,
            "avg_confidence": 0.0,
            "error_types": {},
            "warning_types": {},
            "results": [],
        }

        total_confidence = 0
        error_counter: Dict[str, int] = {}
        warning_counter: Dict[str, int] = {}

        for signal in signals:
            validation_result = ValidationEngine.validate(signal, config, current_time)

            if validation_result.is_valid:
                results["valid"] += 1
            else:
                results["invalid"] += 1

            total_confidence += validation_result.confidence_score

            # Count error and warning types
            for error in validation_result.errors:
                error_key = error.split("(")[0].strip()  # Get first part before params
                error_counter[error_key] = error_counter.get(error_key, 0) + 1

            for warning in validation_result.warnings:
                warning_key = warning.split("(")[0].strip()
                warning_counter[warning_key] = warning_counter.get(warning_key, 0) + 1

            results["results"].append({
                "signal_id": signal.id,
                "symbol": signal.symbol,
                "is_valid": validation_result.is_valid,
                "confidence": validation_result.confidence_score,
                "errors": validation_result.errors,
                "warnings": validation_result.warnings,
            })

        if signals:
            results["avg_confidence"] = total_confidence / len(signals)

        results["error_types"] = error_counter
        results["warning_types"] = warning_counter

        return results
