import os

from PyQt5.QtCore import QUrl
from PyQt5.QtMultimedia import QSoundEffect

class SoundPlayer:
    def __init__(self):
        self._effects = {}

    def play(self, path: str, volume: float = 0.8):
        path = os.path.abspath(path)
        eff = self._effects.get(path)
        if eff is None:
            eff = QSoundEffect()
            eff.setSource(QUrl.fromLocalFile(path))
            self._effects[path] = eff
        eff.setVolume(volume)
        eff.play()
