from io import BytesIO
from typing import Tuple

from pydub import AudioSegment
from voxpopuli import Voice

from utils import GTTS_ESPEAK_DICT


def make_espeak(text: str, lang: str) -> Tuple[bytes, float]:
    voice = Voice(lang=GTTS_ESPEAK_DICT[lang], speed=130, volume=2) if lang in GTTS_ESPEAK_DICT else Voice(lang="en",speed=130)
    wav = voice.to_audio(text)

    pydub_wav = AudioSegment.from_file_using_temporary_files(BytesIO(wav))
    audio_length = len(pydub_wav)/1000

    return wav, audio_length

if __name__ == "__main__":
    print("Not for running directly, this file handles making espeak")
