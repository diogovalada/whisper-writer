import os
import signal
import subprocess
import time
import sys
from pynput.keyboard import Controller as PynputController, Key

from utils import ConfigManager


def run_command_or_exit_on_failure(command):
    """
    Run a shell command and exit if it fails.

    Args:
        command (list): The command to run as a list of strings.
    """
    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as e:
        print(f"Error running command: {e}")
        exit(1)

class InputSimulator:
    """
    A class to simulate keyboard input using various methods.
    """

    def __init__(self):
        """
        Initialize the InputSimulator with the specified configuration.
        """
        self.input_method = ConfigManager.get_config_value('post_processing', 'input_method')
        self.dotool_process = None

        if self.input_method in ('pynput', 'clipboard'):
            self.keyboard = PynputController()
        elif self.input_method == 'dotool':
            self._initialize_dotool()

    def _initialize_dotool(self):
        """
        Initialize the dotool process for input simulation.
        """
        self.dotool_process = subprocess.Popen("dotool", stdin=subprocess.PIPE, text=True)
        assert self.dotool_process.stdin is not None

    def _terminate_dotool(self):
        """
        Terminate the dotool process if it's running.
        """
        if self.dotool_process:
            os.kill(self.dotool_process.pid, signal.SIGINT)
            self.dotool_process = None

    def typewrite(self, text):
        """
        Simulate typing the given text with the specified interval between keystrokes.

        Args:
            text (str): The text to type.
        """
        preview = text.replace("\n", "\\n")
        if len(preview) > 120:
            preview = preview[:120] + "…"
        ConfigManager.console_print(f"Inserting via {self.input_method}: {preview}")

        interval = ConfigManager.get_config_value('post_processing', 'writing_key_press_delay')
        if self.input_method == 'pynput':
            self._typewrite_pynput(text, interval)
        elif self.input_method == 'clipboard':
            paste_delay = ConfigManager.get_config_value('post_processing', 'clipboard_paste_delay')
            if paste_delay is None:
                paste_delay = 0.03

            restore_clipboard = ConfigManager.get_config_value('post_processing', 'restore_clipboard')
            if restore_clipboard is None:
                restore_clipboard = True

            self._paste_text(
                text,
                typing_interval=interval,
                paste_delay=paste_delay,
                restore_clipboard=restore_clipboard,
            )
        elif self.input_method == 'ydotool':
            self._typewrite_ydotool(text, interval)
        elif self.input_method == 'dotool':
            self._typewrite_dotool(text, interval)

    def _typewrite_pynput(self, text, interval):
        """
        Simulate typing using pynput.

        Args:
            text (str): The text to type.
            interval (float): The interval between keystrokes in seconds.
        """
        for char in text:
            self.keyboard.press(char)
            self.keyboard.release(char)
            time.sleep(interval)

    def _typewrite_ydotool(self, text, interval):
        """
        Simulate typing using ydotool.

        Args:
            text (str): The text to type.
            interval (float): The interval between keystrokes in seconds.
        """
        cmd = "ydotool"
        run_command_or_exit_on_failure([
            cmd,
            "type",
            "--key-delay",
            str(interval * 1000),
            "--",
            text,
        ])

    def _typewrite_dotool(self, text, interval):
        """
        Simulate typing using dotool.

        Args:
            text (str): The text to type.
            interval (float): The interval between keystrokes in seconds.
        """
        assert self.dotool_process and self.dotool_process.stdin
        self.dotool_process.stdin.write(f"typedelay {interval * 1000}\n")
        self.dotool_process.stdin.write(f"type {text}\n")
        self.dotool_process.stdin.flush()

    def _paste_text(self, text, *, typing_interval, paste_delay, restore_clipboard):
        """
        Simulate text insertion by copying to the clipboard and pasting.

        Args:
            text (str): The text to paste.
            typing_interval (float): Typing delay used only for fallback to typing.
            paste_delay (float): Delay after pasting (used before restoring the clipboard).
            restore_clipboard (bool): Whether to restore previous clipboard contents.
        """
        try:
            import pyperclip
        except Exception as e:
            print(f"Clipboard input method unavailable (pyperclip import failed: {e}). Falling back to typing.")
            self._typewrite_pynput(text, typing_interval)
            return

        previous_clipboard = None
        if restore_clipboard:
            try:
                previous_clipboard = pyperclip.paste()
            except Exception:
                previous_clipboard = None

        try:
            pyperclip.copy(text)
        except Exception as e:
            print(f"Unable to copy text to clipboard ({e}). Falling back to typing.")
            self._typewrite_pynput(text, typing_interval)
            return

        modifier_key = Key.cmd if sys.platform == 'darwin' else Key.ctrl
        with self.keyboard.pressed(modifier_key):
            self.keyboard.press('v')
            self.keyboard.release('v')

        if restore_clipboard and previous_clipboard is not None:
            time.sleep(paste_delay)

            try:
                pyperclip.copy(previous_clipboard)
            except Exception:
                pass

    def cleanup(self):
        """
        Perform cleanup operations, such as terminating the dotool process.
        """
        if self.input_method == 'dotool':
            self._terminate_dotool()
