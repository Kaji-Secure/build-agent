"""What a CPU is, from what the kernel says of it (``/proc/cpuinfo``, ``/sys``): read on each node by the probe Job (nodeprobe.py).

The model name alone says little: a virtual machine shows a generic ``AMD EPYC Processor (with IBPB)`` whatever the real chip is. The **family, model and
stepping** identify the generation (Rome, Milan, Ice Lake ...), ``cpu MHz`` and the cpufreq maximum say how fast it runs, and the cores, threads, cache and
instruction sets say what it can do; ``hypervisor`` and the DMI strings say whether it is a VM and of what. ``parse`` takes the text the probe prints."""

import re

# x86 server and desktop generations by (vendor, family, model): only those that are certain; others show the family and model numbers.
X86 = {
    ("AuthenticAMD", 23, 1): "Naples (Zen)",
    ("AuthenticAMD", 23, 8): "Pinnacle Ridge (Zen+)",
    ("AuthenticAMD", 23, 49): "Rome (Zen 2)",
    ("AuthenticAMD", 23, 96): "Renoir (Zen 2)",
    ("AuthenticAMD", 23, 113): "Matisse (Zen 2)",
    ("AuthenticAMD", 25, 1): "Milan (Zen 3)",
    ("AuthenticAMD", 25, 17): "Genoa (Zen 4)",
    ("AuthenticAMD", 25, 33): "Vermeer (Zen 3)",
    ("AuthenticAMD", 25, 80): "Cezanne (Zen 3)",
    ("AuthenticAMD", 25, 97): "Raphael (Zen 4)",
    ("AuthenticAMD", 25, 116): "Phoenix (Zen 4)",
    ("AuthenticAMD", 25, 117): "Hawk Point (Zen 4)",
    ("AuthenticAMD", 26, 2): "Turin (Zen 5)",
    ("GenuineIntel", 6, 45): "Sandy Bridge-EP",
    ("GenuineIntel", 6, 62): "Ivy Bridge-EP",
    ("GenuineIntel", 6, 63): "Haswell-EP",
    ("GenuineIntel", 6, 79): "Broadwell-EP",
    ("GenuineIntel", 6, 85): "Skylake-SP / Cascade Lake",
    ("GenuineIntel", 6, 94): "Skylake",
    ("GenuineIntel", 6, 106): "Ice Lake-SP",
    ("GenuineIntel", 6, 143): "Sapphire Rapids",
    ("GenuineIntel", 6, 158): "Coffee Lake",
    ("GenuineIntel", 6, 140): "Tiger Lake-U",
    ("GenuineIntel", 6, 141): "Tiger Lake-H",
    ("GenuineIntel", 6, 151): "Alder Lake-S",
    ("GenuineIntel", 6, 154): "Alder Lake-P",
    ("GenuineIntel", 6, 170): "Meteor Lake",
    ("GenuineIntel", 6, 183): "Raptor Lake-S",
    ("GenuineIntel", 6, 186): "Raptor Lake-P/U",
    ("GenuineIntel", 6, 207): "Emerald Rapids",
}
# Arm cores by (implementer, part): their hex numbers as the kernel prints them.
ARM = {
    ("0x41", "0xd03"): "Cortex-A53",
    ("0x41", "0xd05"): "Cortex-A55",
    ("0x41", "0xd07"): "Cortex-A57",
    ("0x41", "0xd08"): "Cortex-A72",
    ("0x41", "0xd09"): "Cortex-A73",
    ("0x41", "0xd0b"): "Cortex-A76",
    ("0x41", "0xd0c"): "Neoverse N1",
    ("0x41", "0xd40"): "Neoverse V1",
    ("0x41", "0xd49"): "Neoverse N2",
    ("0x41", "0xd4f"): "Neoverse V2",
    ("0x61", "0x022"): "Apple M1 Firestorm",
}
# The instruction sets worth showing (what a build can use, or cannot), as the kernel names them.
FLAGS_SHOWN = ("sse4_2", "avx", "avx2", "avx512f", "avx512_vnni", "amx_tile", "aes", "sha_ni")


def _int(text, base=10):
    try:
        return int(str(text).strip(), base)
    except (TypeError, ValueError):
        return None


def _float(text):
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return None


def codename(vendor, family, model, implementer="", part=""):
    return X86.get((vendor, family, model)) or ARM.get((implementer.lower(), part.lower())) or ""


def parse(text):
    """The CPU as a dict (only what is known) from the probe's output: the first block of ``/proc/cpuinfo``, then ``processors``, ``cores``,
    ``max_khz``, ``dmi_vendor`` and ``dmi_product`` lines."""
    fields = {}
    for line in (text or "").splitlines():
        key, sep, value = line.partition(":")
        if sep and (key := key.strip()) and key not in fields:
            fields[key] = value.strip()
    vendor, family, model = fields.get("vendor_id", ""), _int(fields.get("cpu family")), _int(fields.get("model"))
    flags = set((fields.get("flags") or fields.get("Features") or "").split())
    cache = re.match(r"(\d+)\s*([KMG])B", fields.get("cache size", ""), re.I)
    cache_kb = int(cache.group(1)) * {"K": 1, "M": 1024, "G": 1024 * 1024}[cache.group(2).upper()] if cache else None
    max_khz = _int(fields.get("max_khz"))
    machine = " ".join(x for x in (fields.get("dmi_vendor", ""), fields.get("dmi_product", "")) if x)
    out = {
        "model": " ".join((fields.get("model name") or fields.get("Model name") or fields.get("Hardware") or fields.get("cpu model") or "").split()),
        "vendor": vendor,
        "family": family,
        "model_id": model,
        "stepping": _int(fields.get("stepping")),
        "codename": codename(vendor, family or 0, model or 0, fields.get("CPU implementer", ""), fields.get("CPU part", "")),
        "mhz": _float(fields.get("cpu MHz")),
        "max_mhz": round(max_khz / 1000, 1) if max_khz else None,
        "cache_kb": cache_kb,
        "processors": _int(fields.get("processors")),
        "cores": _int(fields.get("cores")) or None,
        "virtual": "hypervisor" in flags,
        "machine": machine,
        "flags": [f for f in FLAGS_SHOWN if f in flags],
    }
    if not out["model"] and out["codename"]:
        out["model"] = out["codename"]
    return {k: v for k, v in out.items() if v not in (None, "", []) and not (k == "virtual" and v is False)}
