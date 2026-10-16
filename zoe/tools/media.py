"""Media tools for Zoe (screenshots, music).

Cross-platform via subprocess argument lists, never a shell. Tools return
text and stay silent: the conversation layer speaks the reply, so nothing
here talks over it.
"""

import datetime
import subprocess
import sys
import urllib.parse
import webbrowser

from zoe.config import SCREENSHOT_DIR
from zoe.logger import logger
from zoe.providers.base import (
    SOURCE_SCREEN,
    image_part,
)


def open_file_or_directory(path) -> None:
    """Open a file or directory with the native OS viewer. Never raises."""
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        elif sys.platform.startswith("linux"):
            subprocess.run(["xdg-open", str(path)], check=False)
        elif sys.platform == "win32":
            import os
            os.startfile(str(path))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Could not open {path!r}: {e}")


def take_screenshot(filename: str = None, *, return_image_part: bool = False) -> str | dict:
    """Capture a screenshot into the project's data directory.

    Returns the saved path, or "" when capture failed. A silent failure here
    would look like a successful screenshot.

    If ``return_image_part`` is True, also returns a provider-neutral image
    part with the ``SOURCE_SCREEN`` label so the model can immediately analyze
    it. This is the recommended way to get a screenshot into the conversation.

    Args:
        filename: Optional filename for the screenshot. Defaults to a timestamped name.
        return_image_part: If True, return a dict with both 'path' and 'image_part'
            (provider-neutral attachment with SOURCE_SCREEN label). If False (default),
            return only the path string for backward compatibility.

    Returns:
        str: Path to the saved screenshot (when return_image_part=False).
        dict: {'path': str, 'image_part': dict} with SOURCE_SCREEN label (when True).
    """
    try:
        import pyautogui
    except ImportError:
        logger.warning("pyautogui is not installed; cannot capture a screenshot.")
        return "" if not return_image_part else {"path": "", "image_part": None}

    name = (filename or "").strip()
    if not name:
        name = f"screenshot_{datetime.datetime.now():%Y%m%d_%H%M%S}.png"
    elif not name.lower().endswith(".png"):
        name = f"{name}.png"

    # The filename came from a model, so keep it inside the screenshot
    # directory: `../../` must not escape it.
    save_path = (SCREENSHOT_DIR / name).resolve()
    if not str(save_path).startswith(str(SCREENSHOT_DIR.resolve())):
        logger.warning(f"Rejected screenshot filename with a path in it: {filename!r}")
        return "" if not return_image_part else {"path": "", "image_part": None}

    try:
        pyautogui.screenshot().save(str(save_path))
    except Exception as e:  # noqa: BLE001
        logger.error(f"Screenshot capture failed: {e}", exc_info=True)
        return "" if not return_image_part else {"path": "", "image_part": None}

    logger.info(f"Screenshot saved: {save_path}")
    open_file_or_directory(save_path)

    if return_image_part:
        # Read the saved file and create an image part with SOURCE_SCREEN label
        try:
            with open(save_path, "rb") as f:
                img_data = f.read()
            part = image_part(img_data, "image/png", source=SOURCE_SCREEN)
            return {"path": str(save_path), "image_part": part}
        except Exception as e:
            logger.error(f"Failed to create image part for screenshot: {e}")
            return {"path": str(save_path), "image_part": None}

    return str(save_path)


def play_music(song_name: str = None) -> str:
    """Play a track on YouTube, falling back to a YouTube search.

    Returns what actually happened, because the fallback still counts as
    playing something but is not the same as the requested track.
    """
    target = (song_name or "").strip()
    if not target:
        return "I need a song name to play."

    try:
        import pywhatkit
        pywhatkit.playonyt(target)
        logger.info(f"Playing on YouTube: {target!r}")
        return f"Playing {target} on YouTube."
    except Exception as e:  # noqa: BLE001
        logger.warning(f"pywhatkit playback failed for {target!r}: {e}")
        query = urllib.parse.quote_plus(target)
        webbrowser.open(f"https://www.youtube.com/results?search_query={query}")
        return f"I opened a YouTube search for {target}."
