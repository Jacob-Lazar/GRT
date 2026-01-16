"""
Unit tests for sync daemon.
"""

import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from sync.config import SyncConfig
from sync.daemon import SyncDaemon, _CircuitBreaker, CircuitState


class TestSyncConfig(unittest.TestCase):
    """Tests for SyncConfig."""
    
    def test_default_values(self):
        """Test default configuration values."""
        config = SyncConfig()
        self.assertEqual(config.reflect_endpoint, "http://localhost:8000/v1/traces")
        self.assertEqual(config.sync_interval_sec, 5.0)
        self.assertEqual(config.batch_size, 512)
        self.assertTrue(config.enabled)
        self.assertTrue(config.compression)
        self.assertEqual(config.timeout_ms, 30000)
    
    def test_custom_values(self):
        """Test custom configuration values."""
        config = SyncConfig(
            reflect_endpoint="http://custom:9000/traces",
            sync_interval_sec=10.0,
            batch_size=1000,
            enabled=False,
        )
        self.assertEqual(config.reflect_endpoint, "http://custom:9000/traces")
        self.assertEqual(config.sync_interval_sec, 10.0)
        self.assertEqual(config.batch_size, 1000)
        self.assertFalse(config.enabled)
    
    @patch.dict("os.environ", {
        "GRT_SYNC_ENDPOINT": "http://env:8080/v1/traces",
        "GRT_SYNC_INTERVAL_SEC": "15.0",
        "GRT_SYNC_ENABLED": "false",
    })
    def test_from_env(self):
        """Test loading from environment variables."""
        config = SyncConfig.from_env()
        self.assertEqual(config.reflect_endpoint, "http://env:8080/v1/traces")
        self.assertEqual(config.sync_interval_sec, 15.0)
        self.assertFalse(config.enabled)


class TestCircuitBreaker(unittest.TestCase):
    """Tests for CircuitBreaker."""
    
    def test_initial_state_closed(self):
        """Test circuit starts in CLOSED state."""
        cb = _CircuitBreaker()
        self.assertEqual(cb.state, CircuitState.CLOSED)
        self.assertTrue(cb.allow_request())
    
    def test_opens_after_threshold(self):
        """Test circuit opens after consecutive failures."""
        cb = _CircuitBreaker(failure_threshold=3)
        
        # Record 3 failures
        for _ in range(3):
            cb.record_failure()
        
        self.assertEqual(cb.state, CircuitState.OPEN)
        self.assertFalse(cb.allow_request())
    
    def test_success_resets_failures(self):
        """Test success resets failure count."""
        cb = _CircuitBreaker(failure_threshold=3)
        
        cb.record_failure()
        cb.record_failure()
        cb.record_success()  # Reset
        cb.record_failure()
        cb.record_failure()
        
        # Should still be closed (failures reset)
        self.assertEqual(cb.state, CircuitState.CLOSED)


class TestSyncDaemon(unittest.TestCase):
    """Tests for SyncDaemon."""
    
    def test_daemon_disabled(self):
        """Test daemon doesn't start when disabled."""
        config = SyncConfig(enabled=False)
        mock_buffer = MagicMock()
        
        daemon = SyncDaemon(buffer=mock_buffer, config=config)
        daemon.start()
        
        # Thread should not be created
        self.assertIsNone(daemon._thread)
    
    def test_transform_span_basic(self):
        """Test span transformation to OTLP format."""
        mock_buffer = MagicMock()
        daemon = SyncDaemon(buffer=mock_buffer, config=SyncConfig(enabled=False))
        
        # Create mock span
        mock_span = MagicMock()
        mock_span.trace_id = "abc123"
        mock_span.span_id = "def456"
        mock_span.name = "test.span"
        mock_span.kind = 0  # INTERNAL
        mock_span.start_time_ns = 1000000000
        mock_span.end_time_ns = 2000000000
        mock_span.parent_span_id = None
        mock_span.status_code = 1
        mock_span.status_message = None
        mock_span.attributes = {"key": "value"}
        mock_span.events = []
        mock_span.links = []
        
        result = daemon._transform_span(mock_span)
        
        self.assertEqual(result["traceId"], "abc123")
        self.assertEqual(result["spanId"], "def456")
        self.assertEqual(result["name"], "test.span")
        self.assertEqual(result["kind"], 1)  # OTLP is 1-indexed
        self.assertEqual(result["startTimeUnixNano"], "1000000000")
        self.assertEqual(result["endTimeUnixNano"], "2000000000")
    
    def test_attr_to_otlp_types(self):
        """Test attribute conversion for different types."""
        mock_buffer = MagicMock()
        daemon = SyncDaemon(buffer=mock_buffer, config=SyncConfig(enabled=False))
        
        # String
        result = daemon._attr_to_otlp("str_key", "value")
        self.assertEqual(result["value"]["stringValue"], "value")
        
        # Int
        result = daemon._attr_to_otlp("int_key", 42)
        self.assertEqual(result["value"]["intValue"], "42")
        
        # Float
        result = daemon._attr_to_otlp("float_key", 3.14)
        self.assertEqual(result["value"]["doubleValue"], 3.14)
        
        # Bool
        result = daemon._attr_to_otlp("bool_key", True)
        self.assertEqual(result["value"]["boolValue"], True)
    
    def test_stats_property(self):
        """Test stats property returns correct structure."""
        mock_buffer = MagicMock()
        daemon = SyncDaemon(buffer=mock_buffer)
        
        stats = daemon.stats
        
        self.assertIn("total_synced", stats)
        self.assertIn("total_dropped", stats)
        self.assertIn("sync_errors", stats)
        self.assertIn("circuit_state", stats)
        self.assertIn("enabled", stats)
        self.assertIn("endpoint", stats)


if __name__ == "__main__":
    unittest.main()
