from abc import ABC, abstractmethod


class HedgeStrategy(ABC):
    """Abstract base class for hedge strategies."""

    @abstractmethod
    def execute(self, hedge_bot):
        """Execute the hedge strategy using the provided hedge bot instance."""
        pass


class HedgePositionOpenStrategy(HedgeStrategy):
    """Concrete strategy for opening hedge positions."""

    def execute(self, hedge_bot):
        """Execute the hedge position opening strategy."""
        # Custom logic for opening hedge positions can be implemented here
        hedge_bot.logger.info("Executing hedge position open strategy.")


class HedgePositionCloseStrategy(HedgeStrategy):
    """Concrete strategy for closing hedge positions."""

    def execute(self, hedge_bot):
        """Execute the hedge position closing strategy."""
        # Custom logic for closing hedge positions can be implemented here
        hedge_bot.logger.info("Executing hedge position close strategy.")
