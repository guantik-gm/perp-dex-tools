"""
Trading logger with structured output and error handling.
"""

import os
import csv
import logging
from datetime import datetime
import pytz
from decimal import Decimal

def log_trade_to_csv(exchange: str, ticker: str, side: str, price: str, quantity: str):
    """Log trade details to CSV file."""
    timestamp = datetime.now(pytz.UTC).isoformat()
    filename = f"logs/{exchange}_{ticker}_hedge_mode_trades.csv"
    """Initialize CSV file with headers if it doesn't exist."""
    if not os.path.exists(filename):
        with open(filename, 'w', newline='') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(['exchange', 'timestamp', 'side', 'price', 'quantity'])

    with open(filename, 'a', newline='') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow([
            exchange,
            timestamp,
            side,
            price,
            quantity
        ])

class TradingLogger:
    """Enhanced logging with structured output and error handling."""

    def __init__(self, exchange: str, ticker: str, log_to_console: bool = False):
        self.exchange = exchange
        self.ticker = ticker
        # Ensure logs directory exists at the project root
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        logs_dir = os.path.join(project_root, 'logs')
        os.makedirs(logs_dir, exist_ok=True)

        order_file_name = f"{exchange}_{ticker}_orders.csv"
        debug_log_file_name = f"{exchange}_{ticker}_activity.log"

        account_name = os.getenv('ACCOUNT_NAME')
        if account_name:
            order_file_name = f"{exchange}_{ticker}_{account_name}_orders.csv"
            debug_log_file_name = f"{exchange}_{ticker}_{account_name}_activity.log"

        # Log file paths inside logs directory
        self.log_file = os.path.join(logs_dir, order_file_name)
        self.debug_log_file = os.path.join(logs_dir, debug_log_file_name)
        self.timezone = pytz.timezone(os.getenv('TIMEZONE', 'Asia/Shanghai'))
        self.logger = self._setup_logger(log_to_console)

    def _setup_logger(self, log_to_console: bool) -> logging.Logger:
        """Setup the logger with proper configuration."""
        logger = logging.getLogger(f"trading_bot_{self.exchange}_{self.ticker}")
        logger.setLevel(logging.INFO)

        # Prevent propagation to root logger to avoid duplicate messages
        logger.propagate = False

        # Prevent duplicate handlers
        if logger.handlers:
            return logger

        class TimeZoneFormatter(logging.Formatter):
            def __init__(self, fmt=None, datefmt=None, tz=None):
                super().__init__(fmt=fmt, datefmt=datefmt)
                self.tz = tz

            def formatTime(self, record, datefmt=None):
                dt = datetime.fromtimestamp(record.created, tz=self.tz)
                if datefmt:
                    return dt.strftime(datefmt)
                return dt.isoformat()

        formatter = TimeZoneFormatter(
            "%(asctime)s.%(msecs)03d - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            tz=self.timezone
        )

        # File handler
        file_handler = logging.FileHandler(self.debug_log_file)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        # Console handler if requested
        if log_to_console:
            console_handler = logging.StreamHandler()
            console_handler.setLevel(logging.INFO)
            console_handler.setFormatter(formatter)
            logger.addHandler(console_handler)

        return logger

    def log(self, message: str, level: str = "INFO"):
        """Log a message with the specified level."""
        formatted_message = f"[{self.exchange.upper()}_{self.ticker.upper()}] {message}"
        if level.upper() == "DEBUG":
            self.logger.debug(formatted_message)
        elif level.upper() == "INFO":
            self.logger.info(formatted_message)
        elif level.upper() == "WARNING":
            self.logger.warning(formatted_message)
        elif level.upper() == "ERROR":
            self.logger.error(formatted_message)
        else:
            self.logger.info(formatted_message)

    def log_transaction(self, order_id: str, side: str, quantity: Decimal, price: Decimal, status: str, fee: Decimal = None, fee_rate: Decimal = None, liquidity_role: str = None):
        """
        Log a transaction to CSV file.

        Args:
            order_id: Order ID
            side: Trade side (buy/sell)
            quantity: Trade quantity
            price: Trade price
            status: Order status
            fee: Transaction fee (optional, for compatibility)
            fee_rate: Transaction fee rate (optional, for post-verification)
            liquidity_role: Liquidity role (Maker/Taker, optional)
        """
        try:
            timestamp = datetime.now(self.timezone).strftime("%Y-%m-%d %H:%M:%S")
            # Add fee, fee_rate, and liquidity_role to the row, default if not provided
            fee_value = fee if fee is not None else Decimal('0')
            fee_rate_value = fee_rate if fee_rate is not None else Decimal('0')
            liquidity_role_value = liquidity_role if liquidity_role is not None else ''
            row = [timestamp, order_id, side, quantity, price, status, fee_value, fee_rate_value, liquidity_role_value]

            # Check if file exists to write headers
            file_exists = os.path.isfile(self.log_file)

            with open(self.log_file, 'a', newline='', encoding='utf-8') as csvfile:
                writer = csv.writer(csvfile)
                if not file_exists:
                    writer.writerow(['Timestamp', 'OrderID', 'Side', 'Quantity', 'Price', 'Status', 'Fee', 'FeeRate', 'LiquidityRole'])
                writer.writerow(row)

        except Exception as e:
            self.log(f"Failed to log transaction: {e}", "ERROR")
