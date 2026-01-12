"""
gate.cli
~~~~~~~~

Command-line interface for running Python scripts with auto-instrumentation.

Examples:
    !!! example "Basic usage"
        ```bash
        grt-run my_agent.py
        ```
        
    !!! example "With configuration"
        ```bash
        grt-run --endpoint http://collector:4318/v1/traces \\
                --service-name my-agent \\
                --sample-rate 0.1 \\
                my_agent.py
        ```
        
    !!! example "With verbose logging"
        ```bash
        grt-run -v my_agent.py
        ```

Execution Flow:
    1. Parse CLI arguments.
    2. Configure gate (endpoint, sampling, etc.).
    3. Install import hooks BEFORE any user code runs.
    4. Instrument common libraries (OpenAI, httpx, etc.).
    5. Execute the target script.
    6. On exit, flush pending spans.
"""

from __future__ import annotations

import sys
import os
import logging
import argparse
import runpy
import atexit
from typing import Any

from .config import configure, get_config
from .integrations import install_import_hook, patch_existing_base_agent
from .core import shutdown, TracerProvider
from .hooks import instrument_all

__all__ = [
    "main",
    "setup_logging",
    "run_script",
    "cleanup",
]

logger = logging.getLogger("gate")


def setup_logging(verbose: bool = False) -> None:
    """Configure logging for gate."""
    level = logging.DEBUG if verbose else logging.WARNING
    
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)
    formatter = logging.Formatter(
        '[gate] %(levelname)s %(name)s: %(message)s'
    )
    handler.setFormatter(formatter)
    
    # Configure gate logger
    gate_logger = logging.getLogger("gate")
    gate_logger.setLevel(level)
    gate_logger.addHandler(handler)
    gate_logger.propagate = False


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog='grt-run',
        description='Run a Python script with gate auto-instrumentation',
        epilog='''
Examples:
    grt-run my_agent.py
    grt-run --endpoint http://collector:4318/v1/traces my_agent.py --agent-arg
    grt-run --sample-rate 0.1 production_agent.py
        ''',
    )
    
    # gate configuration
    parser.add_argument(
        '--endpoint',
        default=None,
        help='OTLP collector endpoint (default: $GATE_ENDPOINT or http://localhost:4318/v1/traces)',
    )
    parser.add_argument(
        '--service-name',
        default=None,
        help='Service name for traces (default: $GATE_SERVICE_NAME or script name)',
    )
    parser.add_argument(
        '--environment',
        default=None,
        help='Environment tag (default: $GATE_ENVIRONMENT or development)',
    )
    parser.add_argument(
        '--sample-rate',
        type=float,
        default=None,
        help='Sampling rate 0.0-1.0 (default: $GATE_SAMPLE_RATE or 1.0)',
    )
    parser.add_argument(
        '--batch-size',
        type=int,
        default=None,
        help='Spans per export batch (default: 512)',
    )
    parser.add_argument(
        '--buffer-capacity',
        type=int,
        default=None,
        help='Ring buffer capacity (default: 4096)',
    )
    
    # Logging
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Enable verbose logging',
    )
    parser.add_argument(
        '-q', '--quiet',
        action='store_true',
        help='Disable gate logging',
    )
    
    # Disable instrumentation (for debugging)
    parser.add_argument(
        '--disable',
        action='store_true',
        help='Disable all instrumentation (passthrough mode)',
    )
    parser.add_argument(
        '--no-auto-instrument',
        action='store_true',
        help='Disable auto-instrumentation of OpenAI/httpx/etc.',
    )
    
    # Target script (everything after this is passed to the script)
    parser.add_argument(
        'script',
        help='Path to the Python script to run',
    )
    parser.add_argument(
        'script_args',
        nargs=argparse.REMAINDER,
        help='Arguments to pass to the script',
    )
    
    return parser.parse_args()


def main() -> None:
    """
    Main entry point for grt-run CLI.
    
    This is the primary entry point registered in pyproject.toml.
    """
    args = parse_args()
    
    # Setup logging first
    if not args.quiet:
        setup_logging(verbose=args.verbose)
    
    # Handle disable mode
    if args.disable:
        logger.info("Instrumentation disabled, running in passthrough mode")
        run_script(args.script, args.script_args)
        return
    
    # Configure gate
    config_kwargs = {}
    
    if args.endpoint:
        config_kwargs['endpoint'] = args.endpoint
    
    if args.service_name:
        config_kwargs['service_name'] = args.service_name
    else:
        # Use script name as service name if not specified
        script_name = os.path.basename(args.script).replace('.py', '')
        config_kwargs['service_name'] = script_name
    
    if args.environment:
        config_kwargs['environment'] = args.environment
    
    if args.sample_rate is not None:
        config_kwargs['sample_rate'] = args.sample_rate
    
    if args.batch_size is not None:
        config_kwargs['batch_size'] = args.batch_size
    
    if args.buffer_capacity is not None:
        config_kwargs['buffer_capacity'] = args.buffer_capacity
    
    # Apply configuration
    config = configure(**config_kwargs)
    
    if not config.enabled:
        logger.info("gate disabled via configuration")
        run_script(args.script, args.script_args)
        return
    
    logger.info(f"gate configured: endpoint={config.endpoint}, service={config.service_name}")
    
    # Install import hook BEFORE importing any user code
    # This is the critical step that enables zero-code instrumentation
    install_import_hook()
    
    # Also try to patch any agents already imported (edge case)
    patch_existing_base_agent()
    
    # Auto-instrument common libraries
    if not args.no_auto_instrument:
        instrument_all()
    
    # Register shutdown handler to flush spans
    atexit.register(cleanup)
    
    # Run the target script
    logger.info(f"Running script: {args.script}")
    run_script(args.script, args.script_args)


def run_script(script_path: str, script_args: list) -> None:
    """
    Run the target Python script.
    
    Args:
        script_path: Path to the script
        script_args: Arguments to pass to the script
    """
    # Adjust sys.argv so the script sees its own arguments
    sys.argv = [script_path] + script_args
    
    # Add script directory to sys.path
    script_dir = os.path.dirname(os.path.abspath(script_path))
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    
    # Also add current directory
    cwd = os.getcwd()
    if cwd not in sys.path:
        sys.path.insert(0, cwd)
    
    try:
        # Run the script as __main__
        runpy.run_path(script_path, run_name='__main__')
    except SystemExit:
        # Script called sys.exit() - let it propagate
        raise
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(130)
    except Exception as e:
        logger.error(f"Script execution failed: {e}", exc_info=True)
        sys.exit(1)


def cleanup() -> None:
    """Cleanup function called on exit."""
    logger.debug("Flushing pending spans...")
    try:
        shutdown(timeout_ms=5000)
    except Exception as e:
        logger.error(f"Error during shutdown: {e}")


if __name__ == '__main__':
    main()
