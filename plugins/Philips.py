"""Philips WiZ bulb control on the local network.

One tool, one command string: "on", "off", "status", "brightness 60",
"colour 255,0,0", "temperature 4000". The bulb IP comes from WIZ_BULB_IP;
without it the tool discovers bulbs on the LAN.

When no bulb answers, the tool returns NO_BULB and nothing else -- the assistant
must say that sentence verbatim and must not claim the lights changed.
Requires: pip install pywizlight
"""
import asyncio
import os

TOOL = {
    "name": "philips_wiz",
    "description": (
        "Control the Philips WiZ bulb: command is one of 'on', 'off', 'status', "
        "'brightness <0-100>', 'colour <r,g,b>' or 'temperature <2200-6500>'. Use "
        "for every request about the lights. If the result says the bulb was not "
        "detected, relay that sentence word for word and do not claim success."
    ),
    "parameters": {"command": "str"},
}

#: The one sentence the assistant is allowed to say when no bulb answers.
NO_BULB = "Sir, I can't detect any Philips Bulb."

#: Seconds per network step. Two steps plus discovery must stay under
#: plugins.timeout_s (10) in config.yaml, or the loader kills the tool first.
_TIMEOUT = 4


async def _execute(command: str) -> str:
    words = command.split()
    action = words[0].lower() if words else ""
    arg = " ".join(words[1:])

    try:
        from pywizlight import PilotBuilder, discovery, wizlight
    except ImportError:
        return NO_BULB

    ip = os.getenv("WIZ_BULB_IP", "").strip()
    if ip:
        bulb = wizlight(ip)
    else:
        finder = getattr(discovery, "find_wizlights", None) or discovery.discover_lights
        try:
            found = await asyncio.wait_for(finder(), _TIMEOUT)
            bulb = wizlight(found[0].ip) if found else None
        except Exception:  # noqa: BLE001 -- "no bulb there" is the expected case
            bulb = None
    if bulb is None:
        return NO_BULB

    try:
        if action == "on":
            await asyncio.wait_for(bulb.turn_on(PilotBuilder()), _TIMEOUT)
            return "The light is on."
        if action == "off":
            await asyncio.wait_for(bulb.turn_off(), _TIMEOUT)
            return "The light is off."
        if action == "status":
            await asyncio.wait_for(bulb.updateState(), _TIMEOUT)
            return f"The light is {'on' if bulb.state.get_state() else 'off'}."
        if action == "brightness":
            level = int(arg)
            # WiZ brightness is 0-255 internally.
            await asyncio.wait_for(bulb.turn_on(PilotBuilder(brightness=round(level * 255 / 100))), _TIMEOUT)
            return f"Brightness set to {level}%."
        if action == "colour":
            red, green, blue = (int(c) for c in arg.split(","))
            await asyncio.wait_for(bulb.turn_on(PilotBuilder(rgb=(red, green, blue))), _TIMEOUT)
            return "Light colour changed."
        if action == "temperature":
            kelvin = int(arg)
            await asyncio.wait_for(bulb.turn_on(PilotBuilder(colortemp=kelvin)), _TIMEOUT)
            return f"Colour temperature set to {kelvin}K."
    except (asyncio.TimeoutError, OSError):
        # Configured but silent == not connected.
        return NO_BULB
    except (TypeError, ValueError) as exc:
        return f"Bad Philips WiZ command '{command}': {exc}."
    return f"Unknown Philips WiZ command '{command}'."


def run(command: str) -> str:
    """Text the assistant reads back. Never raises: no bulb -> NO_BULB."""
    return asyncio.run(_execute(command))