import io
import os
from typing import Callable

import numpy as np
import soundfile as sf
from faster_whisper import WhisperModel
from openai import OpenAI

from utils import ConfigManager


DEFAULT_MP3_BITRATE_KBPS = 48
DEFAULT_MP3_QUALITY = 2


StatusCallback = Callable[[str], None]


def _emit_status(status_callback: StatusCallback | None, status: str) -> None:
    if status_callback is not None:
        status_callback(status)


class _UploadNotifyingIO(io.BytesIO):
    def __init__(self, initial_bytes: bytes, *, on_upload_complete: Callable[[], None]):
        super().__init__(initial_bytes)
        self._on_upload_complete = on_upload_complete
        self._did_notify = False
        self._total_size = len(initial_bytes)

    def _maybe_notify(self, *, at_eof: bool) -> None:
        if self._did_notify:
            return

        # Some HTTP clients may stop reading immediately after consuming the last bytes
        # (without performing an extra read that returns b''). Treat "position is at end"
        # as upload-complete as well.
        if at_eof or self.tell() >= self._total_size:
            self._did_notify = True
            self._on_upload_complete()

    def read(self, size: int = -1) -> bytes:  # type: ignore[override]
        chunk = super().read(size)
        self._maybe_notify(at_eof=not chunk)
        return chunk

    def readinto(self, b) -> int:  # type: ignore[override]
        n = super().readinto(b)
        self._maybe_notify(at_eof=n == 0)
        return n


def create_local_model():
    """
    Create a local model using the faster-whisper library.
    """
    ConfigManager.console_print('Creating local model...')
    local_model_options = ConfigManager.get_config_section('model_options')['local']
    compute_type = local_model_options['compute_type']
    model_path = local_model_options.get('model_path')

    if compute_type == 'int8':
        device = 'cpu'
        ConfigManager.console_print('Using int8 quantization, forcing CPU usage.')
    else:
        device = local_model_options['device']

    try:
        if model_path:
            ConfigManager.console_print(f'Loading model from: {model_path}')
            model = WhisperModel(model_path,
                                 device=device,
                                 compute_type=compute_type,
                                 download_root=None)  # Prevent automatic download
        else:
            model = WhisperModel(local_model_options['model'],
                                 device=device,
                                 compute_type=compute_type)
    except Exception as e:
        ConfigManager.console_print(f'Error initializing WhisperModel: {e}')
        ConfigManager.console_print('Falling back to CPU.')
        model = WhisperModel(model_path or local_model_options['model'],
                             device='cpu',
                             compute_type=compute_type,
                             download_root=None if model_path else None)

    ConfigManager.console_print('Local model created.')
    return model

def transcribe_local(audio_data, local_model=None):
    """
    Transcribe an audio file using a local model.
    """
    if not local_model:
        local_model = create_local_model()
    model_options = ConfigManager.get_config_section('model_options')

    # Convert int16 to float32
    audio_data_float = audio_data.astype(np.float32) / 32768.0

    response = local_model.transcribe(audio=audio_data_float,
                                      language=model_options['common']['language'],
                                      initial_prompt=model_options['common']['initial_prompt'],
                                      condition_on_previous_text=model_options['local']['condition_on_previous_text'],
                                      temperature=model_options['common']['temperature'],
                                      vad_filter=model_options['local']['vad_filter'],)
    return ''.join([segment.text for segment in list(response[0])])

def _sanitize_mp3_quality(raw_quality):
    try:
        quality = int(raw_quality)
    except (TypeError, ValueError):
        return DEFAULT_MP3_QUALITY
    return min(max(quality, 0), 9)

def _prepare_audio_payload(audio_data, sample_rate, api_options):
    upload_format = (api_options.get('upload_format') or 'wav').lower()

    if upload_format == 'mp3':
        import lameenc

        if audio_data.dtype != np.int16:
            audio_data = audio_data.astype(np.int16, copy=False)

        encoder = lameenc.Encoder()
        encoder.set_bit_rate(DEFAULT_MP3_BITRATE_KBPS)
        encoder.set_in_sample_rate(sample_rate)
        encoder.set_out_sample_rate(sample_rate)
        encoder.set_channels(1)
        encoder.set_quality(_sanitize_mp3_quality(api_options.get('mp3_quality', DEFAULT_MP3_QUALITY)))

        mp3_bytes = encoder.encode(audio_data.tobytes())
        mp3_bytes += encoder.flush()
        byte_io = io.BytesIO(mp3_bytes)
        filename = 'audio.mp3'
        mime_type = 'audio/mpeg'
    else:
        byte_io = io.BytesIO()
        sf.write(byte_io, audio_data, sample_rate, format='wav')
        filename = 'audio.wav'
        mime_type = 'audio/wav'

    byte_io.seek(0)
    return filename, mime_type, byte_io

def transcribe_api(audio_data, *, status_callback: StatusCallback | None = None):
    """
    Transcribe an audio file using the OpenAI API.
    """
    model_options = ConfigManager.get_config_section('model_options')
    api_options = model_options.get('api', {})
    client = OpenAI(
        api_key=os.getenv('OPENAI_API_KEY') or None,
        base_url=model_options['api']['base_url'] or 'https://api.openai.com/v1'
    )

    # Prepare audio payload for the API (WAV or MP3 based on settings)
    sample_rate = ConfigManager.get_config_section('recording_options').get('sample_rate') or 16000

    _emit_status(status_callback, 'encoding')
    upload_format = (api_options.get('upload_format') or 'wav').lower()

    if upload_format == 'mp3':
        uncompressed_wav_io = io.BytesIO()
        sf.write(uncompressed_wav_io, audio_data, sample_rate, format='wav')
        uncompressed_wav_size_bytes = len(uncompressed_wav_io.getvalue())
        ConfigManager.console_print(
            "WAV payload size (uncompressed): "
            f"{uncompressed_wav_size_bytes} bytes ({uncompressed_wav_size_bytes / 1024:.1f} KiB)"
        )

    filename, mime_type, byte_io = _prepare_audio_payload(audio_data, sample_rate, api_options)

    audio_bytes = byte_io.getvalue()
    encoded_size_bytes = len(audio_bytes)
    if upload_format != 'mp3':
        # In WAV mode, the upload payload is the WAV. Still print it explicitly so it's
        # obvious what the pre-compression size is.
        ConfigManager.console_print(
            "WAV payload size: "
            f"{encoded_size_bytes} bytes ({encoded_size_bytes / 1024:.1f} KiB)"
        )
    ConfigManager.console_print(
        f"Upload payload size: {encoded_size_bytes} bytes ({encoded_size_bytes / 1024:.1f} KiB)"
    )

    # Emit 'transcribing' as soon as the HTTP client finishes reading the request body,
    # which is the closest approximation we have to "upload complete".
    def on_upload_complete():
        _emit_status(status_callback, 'transcribing')

    audio_buffer = _UploadNotifyingIO(audio_bytes, on_upload_complete=on_upload_complete)

    _emit_status(status_callback, 'uploading')

    response = client.audio.transcriptions.create(
        model=model_options['api']['model'],
        file=(filename, audio_buffer, mime_type),
        language=model_options['common']['language'],
        prompt=model_options['common']['initial_prompt'],
        temperature=model_options['common']['temperature'],
    )
    return response.text

def post_process_transcription(transcription):
    """
    Apply post-processing to the transcription.
    """
    transcription = transcription.strip()
    post_processing = ConfigManager.get_config_section('post_processing')
    if post_processing['remove_trailing_period'] and transcription.endswith('.'):
        transcription = transcription[:-1]
    if post_processing['add_trailing_space']:
        transcription += ' '
    if post_processing['remove_capitalization']:
        transcription = transcription.lower()

    return transcription

def transcribe(audio_data, local_model=None, *, status_callback: StatusCallback | None = None):
    """
    Transcribe audio date using the OpenAI API or a local model, depending on config.
    """
    if audio_data is None:
        return ''

    if ConfigManager.get_config_value('model_options', 'use_api'):
        transcription = transcribe_api(audio_data, status_callback=status_callback)
    else:
        _emit_status(status_callback, 'transcribing')
        transcription = transcribe_local(audio_data, local_model)

    return post_process_transcription(transcription)
