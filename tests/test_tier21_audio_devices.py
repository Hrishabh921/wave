"""Tier 21 item: "real mic and speaker picker with a level test" (setup
wizard). Tests voice/audio_devices.py directly - no PyQt6 involved, this
module has no UI dependency at all, so it's exercised with real Python and
mocked pyaudio/speech_recognition rather than the fake-Qt harness.
"""
import unittest
from unittest import mock

import _phase4_support as S  # noqa: F401  (adds the project root to sys.path)
import voice.audio_devices as AD


class ListInputDevicesTests(unittest.TestCase):
    def test_returns_enumerated_names_when_sr_available(self):
        with mock.patch.object(AD, "HAS_SR", True), \
             mock.patch.object(AD, "sr", mock.Mock(Microphone=mock.Mock(
                 list_microphone_names=lambda: ["Built-in Mic", "USB Headset"]))):
            self.assertEqual(AD.list_input_devices(), [(0, "Built-in Mic"), (1, "USB Headset")])

    def test_empty_list_when_sr_not_installed(self):
        with mock.patch.object(AD, "HAS_SR", False):
            self.assertEqual(AD.list_input_devices(), [])

    def test_empty_list_when_enumeration_raises(self):
        with mock.patch.object(AD, "HAS_SR", True), \
             mock.patch.object(AD, "sr", mock.Mock(Microphone=mock.Mock(
                 list_microphone_names=mock.Mock(side_effect=OSError("no audio subsystem"))))):
            self.assertEqual(AD.list_input_devices(), [])


class ListOutputDevicesTests(unittest.TestCase):
    def _fake_pyaudio(self, devices):
        pa = mock.Mock()
        pa.get_device_count.return_value = len(devices)
        pa.get_device_info_by_index.side_effect = lambda i: devices[i]
        mod = mock.Mock()
        mod.PyAudio.return_value = pa
        return mod, pa

    def test_filters_to_output_capable_devices_only(self):
        devices = [{"name": "Mic", "maxOutputChannels": 0}, {"name": "Speakers", "maxOutputChannels": 2}]
        mod, pa = self._fake_pyaudio(devices)
        with mock.patch.object(AD, "HAS_PYAUDIO", True), mock.patch.object(AD, "pyaudio", mod):
            self.assertEqual(AD.list_output_devices(), [(1, "Speakers")])
        pa.terminate.assert_called_once()

    def test_empty_list_when_pyaudio_not_installed(self):
        with mock.patch.object(AD, "HAS_PYAUDIO", False):
            self.assertEqual(AD.list_output_devices(), [])

    def test_empty_list_and_no_crash_when_enumeration_raises(self):
        mod = mock.Mock()
        mod.PyAudio.side_effect = OSError("no audio subsystem")
        with mock.patch.object(AD, "HAS_PYAUDIO", True), mock.patch.object(AD, "pyaudio", mod):
            self.assertEqual(AD.list_output_devices(), [])


class MeasureInputLevelTests(unittest.TestCase):
    def test_zero_when_pyaudio_not_installed(self):
        with mock.patch.object(AD, "HAS_PYAUDIO", False):
            self.assertEqual(AD.measure_input_level(), 0.0)

    def test_louder_audio_yields_a_higher_reading(self):
        def make_stream(samples_per_chunk_value):
            stream = mock.Mock()
            chunk = array_from_constant(samples_per_chunk_value, 1024)
            stream.read.return_value = chunk
            return stream

        quiet_pa = mock.Mock()
        quiet_pa.open.return_value = make_stream(100)
        loud_pa = mock.Mock()
        loud_pa.open.return_value = make_stream(20000)

        mod = mock.Mock()
        mod.paInt16 = 8  # arbitrary constant, unused by the mock stream

        with mock.patch.object(AD, "HAS_PYAUDIO", True), mock.patch.object(AD, "pyaudio", mod):
            mod.PyAudio.return_value = quiet_pa
            quiet_level = AD.measure_input_level(duration=0.05)
            mod.PyAudio.return_value = loud_pa
            loud_level = AD.measure_input_level(duration=0.05)
        self.assertGreater(loud_level, quiet_level)
        self.assertLessEqual(loud_level, 100.0)

    def test_zero_and_no_crash_when_stream_open_raises(self):
        mod = mock.Mock()
        pa = mock.Mock()
        pa.open.side_effect = OSError("device busy")
        mod.PyAudio.return_value = pa
        with mock.patch.object(AD, "HAS_PYAUDIO", True), mock.patch.object(AD, "pyaudio", mod):
            self.assertEqual(AD.measure_input_level(), 0.0)
        pa.terminate.assert_called_once()


def array_from_constant(value, n):
    from array import array
    return array("h", [value] * n).tobytes()


class PlayTestToneTests(unittest.TestCase):
    def test_false_when_pyaudio_not_installed(self):
        with mock.patch.object(AD, "HAS_PYAUDIO", False):
            self.assertFalse(AD.play_test_tone())

    def test_true_on_a_successful_write(self):
        pa = mock.Mock()
        stream = mock.Mock()
        pa.open.return_value = stream
        mod = mock.Mock()
        mod.PyAudio.return_value = pa
        with mock.patch.object(AD, "HAS_PYAUDIO", True), mock.patch.object(AD, "pyaudio", mod):
            self.assertTrue(AD.play_test_tone(duration=0.01))
        stream.write.assert_called_once()
        stream.stop_stream.assert_called_once()
        pa.terminate.assert_called_once()

    def test_false_and_no_crash_when_playback_raises(self):
        pa = mock.Mock()
        pa.open.side_effect = OSError("no output device")
        mod = mock.Mock()
        mod.PyAudio.return_value = pa
        with mock.patch.object(AD, "HAS_PYAUDIO", True), mock.patch.object(AD, "pyaudio", mod):
            self.assertFalse(AD.play_test_tone())


if __name__ == "__main__":
    unittest.main()
