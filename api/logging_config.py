"""Structured logging configuration for the application."""
import logging
import sys
from pathlib import Path


def setup_logging(
    level: str = "INFO",
    log_file: Path = None,
    format_string: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
) -> None:
    """
    Configure structured logging for the application.
    
    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: Optional path to log file. If None, logs to stdout only.
        format_string: Custom format string for log messages.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)
    
    # Remove existing handlers to avoid duplicates
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    
    # Create cleaner formatter for console
    console_format = "%(asctime)s | %(levelname)-8s | %(message)s"
    console_formatter = logging.Formatter(
        console_format,
        datefmt="%H:%M:%S"
    )
    
    # Console handler - use stdout for uvicorn compatibility
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(console_formatter)
    console_handler.flush = lambda: sys.stdout.flush()
    root_logger.addHandler(console_handler)
    
    # File handler (optional) - use full format
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_formatter = logging.Formatter(format_string)
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(log_level)
        file_handler.setFormatter(file_formatter)
        root_logger.addHandler(file_handler)
    
    # Set specific logger levels to reduce noise
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance with the specified name.
    
    Args:
        name: Logger name (typically __name__ of the calling module)
    
    Returns:
        Configured logger instance
    """
    logger = logging.getLogger(name)
    # Force flush after each log
    original_info = logger.info
    original_error = logger.error
    original_warning = logger.warning
    
    def flush_info(msg, *args, **kwargs):
        original_info(msg, *args, **kwargs)
        sys.stdout.flush()
    
    def flush_error(msg, *args, **kwargs):
        original_error(msg, *args, **kwargs)
        sys.stdout.flush()
    
    def flush_warning(msg, *args, **kwargs):
        original_warning(msg, *args, **kwargs)
        sys.stdout.flush()
    
    logger.info = flush_info
    logger.error = flush_error
    logger.warning = flush_warning
    
    return logger
