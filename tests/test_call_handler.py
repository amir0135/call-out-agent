"""Tests for the ACS Call Handler — mocked SDK interactions."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from azure.communication.callautomation import DtmfTone
from azure.communication.callautomation import SsmlSource, TextSource

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from orchestrator.call_handler import CallHandler
from orchestrator.models import AlarmContext, CallIntent, CallSession


class TestCallHandler:
    """Test call handler with mocked ACS SDK."""

    @patch("orchestrator.call_handler.DefaultAzureCredential")
    @patch("orchestrator.call_handler.CallAutomationClient")
    def test_initiate_call(self, mock_client_cls, mock_cred_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_cred_cls.return_value = MagicMock()

        mock_result = MagicMock()
        mock_result.call_connection_id = "conn-123"
        mock_client.create_call.return_value = mock_result

        handler = CallHandler(
            acs_endpoint="https://test.communication.azure.com",
            acs_phone_number="+10000000000",
            callback_base_url="https://test.example.com",
        )

        alarm = AlarmContext(
            alarm_id="ALM-001",
            alarm_type="high_temperature",
            store_name="Test Store",
            store_id="ST-001",
            equipment_name="Cooler Unit A",
            current_temp=12.5,
            threshold_temp=8.0,
            alarm_time="2026-04-16T10:30:00Z",
            customer_id="CUST-001",
        )
        intent = CallIntent(
            intent_id="INT-001",
            alarm=alarm,
            phone_number="+1234567890",
        )
        session = CallSession(call_id="CALL-001", intent=intent)

        connection_id = handler.initiate_call(session)
        assert connection_id == "conn-123"
        mock_client.create_call.assert_called_once()

    @patch("orchestrator.call_handler.DefaultAzureCredential")
    @patch("orchestrator.call_handler.CallAutomationClient")
    def test_play_text(self, mock_client_cls, mock_cred_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_cred_cls.return_value = MagicMock()

        mock_connection = MagicMock()
        mock_client.get_call_connection.return_value = mock_connection

        handler = CallHandler(
            acs_endpoint="https://test.communication.azure.com",
            acs_phone_number="+10000000000",
            callback_base_url="https://test.example.com",
        )

        handler.play_text("conn-123", "Hello from Contoso.")
        mock_connection.play_media.assert_called_once()

    @patch("orchestrator.call_handler.DefaultAzureCredential")
    @patch("orchestrator.call_handler.CallAutomationClient")
    def test_session_management(self, mock_client_cls, mock_cred_cls):
        mock_client_cls.return_value = MagicMock()
        mock_cred_cls.return_value = MagicMock()

        handler = CallHandler(
            acs_endpoint="https://test.communication.azure.com",
            acs_phone_number="+10000000000",
            callback_base_url="https://test.example.com",
        )

        alarm = AlarmContext(
            alarm_id="ALM-001",
            alarm_type="high_temperature",
            store_name="Test Store",
            store_id="ST-001",
            equipment_name="Cooler A",
            current_temp=12.0,
            threshold_temp=8.0,
            alarm_time="2026-04-16T10:30:00Z",
            customer_id="CUST-001",
        )
        intent = CallIntent(intent_id="INT-001", alarm=alarm, phone_number="+1234567890")
        session = CallSession(call_id="CALL-001", intent=intent)

        handler.register_session(session)
        assert handler.get_session("CALL-001") is session

        removed = handler.remove_session("CALL-001")
        assert removed is session
        assert handler.get_session("CALL-001") is None

    @patch("orchestrator.call_handler.DefaultAzureCredential")
    @patch("orchestrator.call_handler.CallAutomationClient")
    def test_hang_up(self, mock_client_cls, mock_cred_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_cred_cls.return_value = MagicMock()

        mock_connection = MagicMock()
        mock_client.get_call_connection.return_value = mock_connection

        handler = CallHandler(
            acs_endpoint="https://test.communication.azure.com",
            acs_phone_number="+10000000000",
            callback_base_url="https://test.example.com",
        )

        handler.hang_up("conn-123")
        mock_connection.hang_up.assert_called_once_with(is_for_everyone=True)

    @patch("orchestrator.call_handler.DefaultAzureCredential")
    @patch("orchestrator.call_handler.CallAutomationClient")
    def test_start_recording_uses_callback_url(self, mock_client_cls, mock_cred_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_cred_cls.return_value = MagicMock()

        mock_recording = MagicMock()
        mock_recording.recording_id = "rec-123"
        mock_client.start_recording.return_value = mock_recording

        handler = CallHandler(
            acs_endpoint="https://test.communication.azure.com",
            acs_phone_number="+10000000000",
            callback_base_url="https://test.example.com",
        )

        recording_id = handler.start_recording(
            "server-call-123",
            recording_state_callback_url="https://test.example.com/api/acs-callback?callId=CALL-001",
        )

        assert recording_id == "rec-123"
        mock_client.start_recording.assert_called_once_with(
            server_call_id="server-call-123",
            recording_state_callback_url="https://test.example.com/api/acs-callback?callId=CALL-001",
        )


class TestRecognizeConfiguration:
    """Verify that the recognize call uses listen-quality defaults."""

    @patch("orchestrator.call_handler.DefaultAzureCredential")
    @patch("orchestrator.call_handler.CallAutomationClient")
    def test_recognize_uses_dtmf_fallback_by_default(self, mock_client_cls, mock_cred_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_cred_cls.return_value = MagicMock()
        mock_connection = MagicMock()
        mock_client.get_call_connection.return_value = mock_connection

        handler = CallHandler(
            acs_endpoint="https://test.communication.azure.com",
            acs_phone_number="+10000000000",
            callback_base_url="https://test.example.com",
        )

        handler.start_recognize_speech(
            "conn-123",
            "Are you aware of this alarm?",
            target_phone="+15555550100",
            context="sop_ask",
        )

        mock_connection.start_recognizing_media.assert_called_once()
        kwargs = mock_connection.start_recognizing_media.call_args.kwargs
        # DTMF fallback path must be enabled so a keypress always works.
        assert kwargs["input_type"] == "speechOrDtmf"
        assert kwargs["dtmf_max_tones_to_collect"] == 1
        # ACS requires the DtmfTone enum value; literal ``"#"`` is
        # rejected by the server with HTTP 400.
        assert kwargs["dtmf_stop_tones"] == [DtmfTone.POUND]
        # ACS requires integer seconds; 1.5 is rounded to 2 at the boundary.
        # (See call_handler.start_recognize_speech for the cast.)
        assert kwargs["end_silence_timeout"] == 2
        assert isinstance(kwargs["end_silence_timeout"], int)
        # Barge-in stays on so callers can interrupt the prompt.
        assert kwargs["interrupt_prompt"] is True

    @patch("orchestrator.call_handler.DefaultAzureCredential")
    @patch("orchestrator.call_handler.CallAutomationClient")
    def test_recognize_dtmf_fallback_can_be_disabled(self, mock_client_cls, mock_cred_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_cred_cls.return_value = MagicMock()
        mock_connection = MagicMock()
        mock_client.get_call_connection.return_value = mock_connection

        handler = CallHandler(
            acs_endpoint="https://test.communication.azure.com",
            acs_phone_number="+10000000000",
            callback_base_url="https://test.example.com",
            enable_dtmf_fallback=False,
        )

        handler.start_recognize_speech(
            "conn-123",
            "Are you aware?",
            target_phone="+15555550100",
        )

        kwargs = mock_connection.start_recognizing_media.call_args.kwargs
        assert kwargs["input_type"] == "speech"
        assert "dtmf_max_tones_to_collect" not in kwargs

    @patch("orchestrator.call_handler.DefaultAzureCredential")
    @patch("orchestrator.call_handler.CallAutomationClient")
    def test_recognize_interrupt_prompt_override(self, mock_client_cls, mock_cred_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_cred_cls.return_value = MagicMock()
        mock_connection = MagicMock()
        mock_client.get_call_connection.return_value = mock_connection

        handler = CallHandler(
            acs_endpoint="https://test.communication.azure.com",
            acs_phone_number="+10000000000",
            callback_base_url="https://test.example.com",
            interrupt_prompt=False,
        )

        handler.start_recognize_speech(
            "conn-123",
            "Are you aware?",
            target_phone="+15555550100",
            interrupt_prompt_override=True,
        )

        kwargs = mock_connection.start_recognizing_media.call_args.kwargs
        assert kwargs["interrupt_prompt"] is True


class TestTtsPronunciation:
    """Verify brand-name pronunciation handling in TTS source builder."""

    @patch("orchestrator.call_handler.DefaultAzureCredential")
    @patch("orchestrator.call_handler.CallAutomationClient")
    def test_contoso_uses_fast_text_source_with_normalization(self, mock_client_cls, mock_cred_cls):
        mock_client_cls.return_value = MagicMock()
        mock_cred_cls.return_value = MagicMock()

        handler = CallHandler(
            acs_endpoint="https://test.communication.azure.com",
            acs_phone_number="+10000000000",
            callback_base_url="https://test.example.com",
        )

        source = handler._build_text_source("Hello from Contoso monitoring.")

        assert isinstance(source, TextSource)
        assert source.text == "Hello from Con-toso monitoring."

    @patch("orchestrator.call_handler.DefaultAzureCredential")
    @patch("orchestrator.call_handler.CallAutomationClient")
    def test_non_brand_text_uses_plain_text_source_by_default(self, mock_client_cls, mock_cred_cls):
        mock_client_cls.return_value = MagicMock()
        mock_cred_cls.return_value = MagicMock()

        handler = CallHandler(
            acs_endpoint="https://test.communication.azure.com",
            acs_phone_number="+10000000000",
            callback_base_url="https://test.example.com",
        )

        source = handler._build_text_source("Hello from the monitoring center.")

        assert isinstance(source, TextSource)
