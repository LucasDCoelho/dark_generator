"""Escolhe a voz de cada personagem (ElevenLabs ou edge-tts), de forma estável ao longo da série."""

from __future__ import annotations

NARRATOR_NAMES = {"NARRADOR", "NARRADORA", "NARRATOR", "NARRACIÓN", "NARRACION"}


def is_narrator(speaker: str) -> bool:
    return (speaker or "").strip().upper() in NARRATOR_NAMES or not (speaker or "").strip()


def assign_voices(characters: list[dict], settings: dict) -> dict[str, str]:
    """Atribui uma voz da ElevenLabs a cada personagem, alternando dentro do gênero.
    Vozes ficam salvas na ficha da série, então o personagem soa igual em todos os episódios."""
    pools = {
        "male": list(settings.get("eleven_male_voices") or []),
        "female": list(settings.get("eleven_female_voices") or []),
    }
    narrator = settings.get("eleven_narrator_voice")
    used: dict[str, int] = {"male": 0, "female": 0}
    out: dict[str, str] = {}
    for ch in characters:
        gender = "female" if (ch.get("gender") or "").lower().startswith(("f", "w")) else "male"
        pool = [v for v in pools[gender] if v != narrator] or pools[gender] or [narrator]
        out[ch["name"]] = pool[used[gender] % len(pool)]
        used[gender] += 1
    return out


def voice_for(speaker: str, provider: str, settings: dict, bible: dict | None) -> str:
    bible = bible or {}
    if provider == "elevenlabs":
        if is_narrator(speaker):
            return settings["eleven_narrator_voice"]
        voices = bible.get("voices") or {}
        if speaker in voices:
            return voices[speaker]
        gender = _gender(speaker, bible)
        pool = settings.get(f"eleven_{gender}_voices") or [settings["eleven_narrator_voice"]]
        return pool[hash(speaker) % len(pool)]
    # edge-tts / silencioso: narrador e homens com a voz do canal, mulheres com a voz feminina
    if not is_narrator(speaker) and _gender(speaker, bible) == "female":
        return settings.get("edge_female_voice") or settings["voice"]
    return settings["voice"]


def _gender(speaker: str, bible: dict) -> str:
    for ch in bible.get("characters") or []:
        if ch.get("name") == speaker:
            return "female" if (ch.get("gender") or "").lower().startswith(("f", "w")) else "male"
    return "male"
