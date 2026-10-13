"""Retro game sound effects without a key, a network or a GPU: a port of sfxr (DrPetter's synthesizer, the one
behind jsfxr/bfxr), with its classic presets. Every call rolls new parameters, so the same prompt gives a fresh
variant; pass a seed to repeat one. Output: 44.1 kHz 16-bit mono WAV, free to use (it is generated).
"""
from __future__ import annotations

import io
import math
import random
import wave

RATE = 44100

# keyword -> preset (English and Russian stems)
KEYWORDS = [
    ("explosion", ("explo", "boom", "blast", "bomb", "nuke", "grenade", "взрыв", "бомб", "граната", "ядерн", "бах")),
    ("laser", ("laser", "shot", "shoot", "gun", "blaster", "zap", "fire", "pew", "rifle", "лазер", "выстрел", "стрел",
               "пушк", "бластер", "ружь")),
    ("pickup", ("coin", "pickup", "pick up", "collect", "gem", "money", "item", "монет", "подбор", "собра", "предмет",
                "кристал", "деньг")),
    ("powerup", ("power", "level up", "levelup", "upgrade", "buff", "heal", "magic", "spell", "бонус", "усилен", "уровень",
                 "лечен", "магия", "заклин")),
    ("hit", ("hit", "hurt", "damage", "punch", "impact", "slash", "sword", "удар", "урон", "больно", "меч", "попадан")),
    ("jump", ("jump", "hop", "bounce", "прыж", "отскок")),
    ("blip", ("blip", "select", "click", "menu", "ui", "button", "beep", "клик", "меню", "кнопк", "писк")),
]


def preset_for(prompt: str) -> str:
    p = (prompt or "").lower()
    for name, words in KEYWORDS:
        if any(w in p for w in words):
            return name
    return "blip"


def _params(preset: str, r: random.Random) -> dict:
    frnd = lambda x: r.random() * x  # noqa: E731
    rnd = lambda n: r.randint(0, n)  # noqa: E731
    p = dict(wave_type=0, base_freq=0.3, freq_limit=0.0, freq_ramp=0.0, freq_dramp=0.0, duty=0.0, duty_ramp=0.0,
             vib_strength=0.0, vib_speed=0.0, env_attack=0.0, env_sustain=0.3, env_punch=0.0, env_decay=0.4,
             lpf_resonance=0.0, lpf_freq=1.0, lpf_ramp=0.0, hpf_freq=0.0, hpf_ramp=0.0, pha_offset=0.0, pha_ramp=0.0,
             repeat_speed=0.0, arp_speed=0.0, arp_mod=0.0)
    if preset == "pickup":
        p.update(base_freq=0.4 + frnd(0.5), env_sustain=frnd(0.1), env_decay=0.1 + frnd(0.4), env_punch=0.3 + frnd(0.3))
        if rnd(1):
            p.update(arp_speed=0.5 + frnd(0.2), arp_mod=0.2 + frnd(0.4))
    elif preset == "laser":
        w = rnd(2)
        if w == 2 and rnd(1):
            w = rnd(1)
        p["wave_type"] = w
        p["base_freq"] = 0.5 + frnd(0.5)
        p["freq_limit"] = max(0.2, p["base_freq"] - 0.2 - frnd(0.6))
        p["freq_ramp"] = -0.15 - frnd(0.2)
        if rnd(2) == 0:
            p.update(base_freq=0.3 + frnd(0.6), freq_limit=frnd(0.1), freq_ramp=-0.35 - frnd(0.3))
        if rnd(1):
            p.update(duty=frnd(0.5), duty_ramp=frnd(0.2))
        else:
            p.update(duty=0.4 + frnd(0.5), duty_ramp=-frnd(0.7))
        p.update(env_sustain=0.1 + frnd(0.2), env_decay=frnd(0.4))
        if rnd(1):
            p["env_punch"] = frnd(0.3)
        if rnd(2) == 0:
            p.update(pha_offset=frnd(0.2), pha_ramp=-frnd(0.2))
        if rnd(1):
            p["hpf_freq"] = frnd(0.3)
    elif preset == "explosion":
        p["wave_type"] = 3
        if rnd(1):
            p.update(base_freq=0.1 + frnd(0.4), freq_ramp=-0.1 + frnd(0.4))
        else:
            p.update(base_freq=0.2 + frnd(0.7), freq_ramp=-0.2 - frnd(0.2))
        p["base_freq"] *= p["base_freq"]
        if rnd(4) == 0:
            p["freq_ramp"] = 0.0
        if rnd(2) == 0:
            p["repeat_speed"] = 0.3 + frnd(0.5)
        p.update(env_sustain=0.1 + frnd(0.3), env_decay=frnd(0.5))
        if rnd(1) == 0:
            p.update(pha_offset=-0.3 + frnd(0.9), pha_ramp=-frnd(0.3))
        p["env_punch"] = 0.2 + frnd(0.6)
        if rnd(1):
            p.update(vib_strength=frnd(0.7), vib_speed=frnd(0.6))
        if rnd(2) == 0:
            p.update(arp_speed=0.6 + frnd(0.3), arp_mod=0.8 - frnd(1.6))
    elif preset == "powerup":
        if rnd(1):
            p["wave_type"] = 1
        else:
            p["duty"] = frnd(0.6)
        if rnd(1):
            p.update(base_freq=0.2 + frnd(0.3), freq_ramp=0.1 + frnd(0.4), repeat_speed=0.4 + frnd(0.4))
        else:
            p.update(base_freq=0.2 + frnd(0.3), freq_ramp=0.05 + frnd(0.2))
            if rnd(1):
                p.update(vib_strength=frnd(0.7), vib_speed=frnd(0.6))
        p.update(env_sustain=frnd(0.4), env_decay=0.1 + frnd(0.4))
    elif preset == "hit":
        w = rnd(2)
        p["wave_type"] = 3 if w == 2 else w
        if p["wave_type"] == 0:
            p["duty"] = frnd(0.6)
        p.update(base_freq=0.2 + frnd(0.6), freq_ramp=-0.3 - frnd(0.4), env_sustain=frnd(0.1), env_decay=0.1 + frnd(0.2))
        if rnd(1):
            p["hpf_freq"] = frnd(0.3)
    elif preset == "jump":
        p.update(wave_type=0, duty=frnd(0.6), base_freq=0.3 + frnd(0.3), freq_ramp=0.1 + frnd(0.2),
                 env_sustain=0.1 + frnd(0.3), env_decay=0.1 + frnd(0.2))
        if rnd(1):
            p["hpf_freq"] = frnd(0.3)
        if rnd(1):
            p["lpf_freq"] = 1 - frnd(0.6)
    else:  # blip
        p["wave_type"] = rnd(1)
        if p["wave_type"] == 0:
            p["duty"] = frnd(0.6)
        p.update(base_freq=0.2 + frnd(0.4), env_sustain=0.1 + frnd(0.1), env_decay=frnd(0.2), hpf_freq=0.1)
    return p


def synth(p: dict, r: random.Random, stretch: float = 1.0, max_seconds: float = 6.0) -> list[float]:
    """sfxr's SynthSample loop (8x supersampled), returning floats in [-1, 1]."""
    st = {}

    def reset(restart: bool):
        if not restart:
            st["phase"] = 0
        st["fperiod"] = 100.0 / (p["base_freq"] ** 2 + 0.001)
        st["fmaxperiod"] = 100.0 / (p["freq_limit"] ** 2 + 0.001)
        st["fslide"] = 1.0 - p["freq_ramp"] ** 3 * 0.01
        st["fdslide"] = -(p["freq_dramp"] ** 3) * 0.000001
        st["square_duty"] = 0.5 - p["duty"] * 0.5
        st["square_slide"] = -p["duty_ramp"] * 0.00005
        st["arp_mod"] = 1.0 - p["arp_mod"] ** 2 * 0.9 if p["arp_mod"] >= 0 else 1.0 + p["arp_mod"] ** 2 * 10.0
        st["arp_time"] = 0
        st["arp_limit"] = 0 if p["arp_speed"] == 1.0 else int((1.0 - p["arp_speed"]) ** 2 * 20000 + 32)
        if not restart:
            st["fltp"] = st["fltdp"] = st["fltphp"] = 0.0
            st["fltw"] = p["lpf_freq"] ** 3 * 0.1
            st["fltw_d"] = 1.0 + p["lpf_ramp"] * 0.0001
            st["fltdmp"] = min(0.8, 5.0 / (1.0 + p["lpf_resonance"] ** 2 * 20.0) * (0.01 + st["fltw"]))
            st["flthp"] = p["hpf_freq"] ** 2 * 0.1
            st["flthp_d"] = 1.0 + p["hpf_ramp"] * 0.0003
            st["vib_phase"] = 0.0
            st["vib_speed"] = p["vib_speed"] ** 2 * 0.01
            st["vib_amp"] = p["vib_strength"] * 0.5
            st["env_vol"] = 0.0
            st["env_stage"] = 0
            st["env_time"] = 0
            st["env_length"] = [int(p["env_attack"] ** 2 * 100000 * stretch), int(p["env_sustain"] ** 2 * 100000 * stretch),
                                int(p["env_decay"] ** 2 * 100000 * stretch)]
            fphase = p["pha_offset"] ** 2 * 1020.0
            st["fphase"] = -fphase if p["pha_offset"] < 0 else fphase
            fd = p["pha_ramp"] ** 2
            st["fdphase"] = -fd if p["pha_ramp"] < 0 else fd
            st["iphase"] = abs(int(st["fphase"]))
            st["ipp"] = 0
            st["phaser"] = [0.0] * 1024
            st["noise"] = [r.random() * 2 - 1 for _ in range(32)]
            st["rep_time"] = 0
            st["rep_limit"] = 0 if p["repeat_speed"] == 0 else int((1.0 - p["repeat_speed"]) ** 2 * 20000 + 32)

    reset(False)
    out: list[float] = []
    wave_type = p["wave_type"]
    for _ in range(int(RATE * max_seconds)):
        st["rep_time"] += 1
        if st["rep_limit"] and st["rep_time"] >= st["rep_limit"]:
            st["rep_time"] = 0
            reset(True)
        st["arp_time"] += 1
        if st["arp_limit"] and st["arp_time"] >= st["arp_limit"]:
            st["arp_limit"] = 0
            st["fperiod"] *= st["arp_mod"]
        st["fslide"] += st["fdslide"]
        st["fperiod"] *= st["fslide"]
        if st["fperiod"] > st["fmaxperiod"]:
            st["fperiod"] = st["fmaxperiod"]
            if p["freq_limit"] > 0:
                break
        rfperiod = st["fperiod"]
        if st["vib_amp"] > 0:
            st["vib_phase"] += st["vib_speed"]
            rfperiod = st["fperiod"] * (1.0 + math.sin(st["vib_phase"]) * st["vib_amp"])
        period = max(8, int(rfperiod))
        st["square_duty"] = min(0.5, max(0.0, st["square_duty"] + st["square_slide"]))
        st["env_time"] += 1
        if st["env_time"] > st["env_length"][st["env_stage"]]:
            st["env_time"] = 0
            st["env_stage"] += 1
            if st["env_stage"] == 3:
                break
        ln = max(1, st["env_length"][st["env_stage"]])
        if st["env_stage"] == 0:
            st["env_vol"] = st["env_time"] / ln
        elif st["env_stage"] == 1:
            st["env_vol"] = 1.0 + (1.0 - st["env_time"] / ln) * 2.0 * p["env_punch"]
        else:
            st["env_vol"] = 1.0 - st["env_time"] / ln
        st["fphase"] += st["fdphase"]
        st["iphase"] = min(1023, abs(int(st["fphase"])))
        if st["flthp_d"] != 0:
            st["flthp"] = min(0.1, max(0.00001, st["flthp"] * st["flthp_d"]))
        ssample = 0.0
        for _si in range(8):
            st["phase"] += 1
            if st["phase"] >= period:
                st["phase"] %= period
                if wave_type == 3:
                    st["noise"] = [r.random() * 2 - 1 for _ in range(32)]
            fp = st["phase"] / period
            if wave_type == 0:
                sample = 0.5 if fp < st["square_duty"] else -0.5
            elif wave_type == 1:
                sample = 1.0 - fp * 2
            elif wave_type == 2:
                sample = math.sin(fp * 2 * math.pi)
            else:
                sample = st["noise"][st["phase"] * 32 // period]
            pp = st["fltp"]
            st["fltw"] = min(0.1, max(0.0, st["fltw"] * st["fltw_d"]))
            if p["lpf_freq"] != 1.0:
                st["fltdp"] += (sample - st["fltp"]) * st["fltw"]
                st["fltdp"] -= st["fltdp"] * st["fltdmp"]
            else:
                st["fltp"] = sample
                st["fltdp"] = 0.0
            st["fltp"] += st["fltdp"]
            st["fltphp"] += st["fltp"] - pp
            st["fltphp"] -= st["fltphp"] * st["flthp"]
            sample = st["fltphp"]
            ph = st["phaser"]
            ph[st["ipp"] & 1023] = sample
            sample += ph[(st["ipp"] - st["iphase"] + 1024) & 1023]
            st["ipp"] = (st["ipp"] + 1) & 1023
            ssample += sample * st["env_vol"]
        ssample = ssample / 8 * 0.05 * 2.0 * 0.5 * 4  # sfxr master and sound volume, a little louder for games
        out.append(max(-1.0, min(1.0, ssample)))
    return out


def make_wav(prompt: str, seconds: float | None = None, seed: int | None = None, preset: str | None = None) -> tuple[bytes, dict]:
    """(WAV bytes, info). The preset comes from the prompt's words unless given."""
    preset = preset or preset_for(prompt)
    r = random.Random(seed)
    p = _params(preset, r)
    natural = (p["env_attack"] ** 2 + p["env_sustain"] ** 2 + p["env_decay"] ** 2) * 100000 / RATE
    stretch = 1.0
    if seconds and natural > 0.01:
        stretch = max(0.3, min(float(seconds) / natural, 8.0))
    samples = synth(p, r, stretch, max_seconds=min(float(seconds or 3) * 1.5 + 0.5, 8.0))
    if not samples or max(abs(s) for s in samples) < 0.01:  # an unlucky roll: try once more
        r = random.Random((seed or 0) + 1)
        p = _params(preset, r)
        samples = synth(p, r, stretch)
    peak = max((abs(s) for s in samples), default=0) or 1.0
    gain = 0.9 / peak if peak < 0.5 else 1.0  # quiet rolls are normalised
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"".join(int(max(-1.0, min(1.0, s * gain)) * 32767).to_bytes(2, "little", signed=True) for s in samples))
    return buf.getvalue(), {"preset": preset, "seconds": round(len(samples) / RATE, 2)}
