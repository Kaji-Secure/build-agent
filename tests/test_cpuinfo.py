"""What a CPU is, from what the kernel says of it (cpuinfo.py): the generation, the speed, the cores, the instruction sets, whether it is a VM."""
from kaji_agent import cpuinfo

# A real machine (a laptop with a 13th Gen Intel Core i5), as the probe prints it: the first block of /proc/cpuinfo, then the extras.
LAPTOP = """processor\t: 0
vendor_id\t: GenuineIntel
cpu family\t: 6
model\t\t: 186
model name\t: 13th Gen Intel(R) Core(TM) i5-1335U
stepping\t: 3
cpu MHz\t\t: 3492.027
cache size\t: 12288 KB
physical id\t: 0
siblings\t: 12
core id\t\t: 0
cpu cores\t: 10
flags\t\t: fpu vme sse4_2 avx avx2 aes sha_ni hypervisor_not_here
bogomips\t: 4992.00

processors: 12
cores: 10
max_khz: 4600000
dmi_vendor: LENOVO
dmi_product: 21KG
"""
# A KVM guest: the model name is QEMU's generic one, the family and model say what the host is, the hypervisor flag and the DMI strings say it is a VM.
KVM = """processor\t: 0
vendor_id\t: AuthenticAMD
cpu family\t: 25
model\t\t: 1
model name\t: AMD EPYC Processor (with IBPB)
stepping\t: 1
cpu MHz\t\t: 2445.406
cache size\t: 512 KB
flags\t\t: fpu sse4_2 avx avx2 aes sha_ni hypervisor avx512f
processors: 6
cores: 0
max_khz:
dmi_vendor: QEMU
dmi_product: Standard PC (Q35 + ICH9, 2009)
"""
ARM = "processor\t: 0\nBogoMIPS\t: 50.00\nFeatures\t: fp asimd aes\nCPU implementer\t: 0x41\nCPU part\t: 0xd0c\nprocessors: 4\n"


def test_a_real_machine_is_described_with_its_generation_speed_cores_and_instruction_sets():
    info = cpuinfo.parse(LAPTOP)
    assert info["model"] == "13th Gen Intel(R) Core(TM) i5-1335U" and info["vendor"] == "GenuineIntel"
    assert (info["family"], info["model_id"], info["stepping"]) == (6, 186, 3) and info["codename"] == "Raptor Lake-P/U"
    assert info["mhz"] == 3492.027 and info["max_mhz"] == 4600.0 and info["cache_kb"] == 12288
    assert (info["processors"], info["cores"]) == (12, 10) and info["machine"] == "LENOVO 21KG"
    assert info["flags"] == ["sse4_2", "avx", "avx2", "aes", "sha_ni"] and "virtual" not in info


def test_a_virtual_machine_says_so_and_its_family_and_model_give_the_real_generation():
    info = cpuinfo.parse(KVM)
    assert info["model"] == "AMD EPYC Processor (with IBPB)"  # QEMU's generic name: it says nothing of the chip
    assert info["codename"] == "Milan (Zen 3)" and info["virtual"] is True and info["machine"] == "QEMU Standard PC (Q35 + ICH9, 2009)"
    assert info["mhz"] == 2445.406 and info["cache_kb"] == 512 and info["processors"] == 6
    assert "max_mhz" not in info and "cores" not in info  # what the kernel did not say is left out, not guessed
    assert "avx512f" in info["flags"] and info["flags"] == ["sse4_2", "avx", "avx2", "avx512f", "aes", "sha_ni"]  # in the order they are shown


def test_an_arm_core_is_named_by_its_implementer_and_part():
    info = cpuinfo.parse(ARM)
    assert info["codename"] == "Neoverse N1" and info["model"] == "Neoverse N1" and info["processors"] == 4


def test_an_unknown_generation_keeps_its_numbers_and_has_no_codename():
    info = cpuinfo.parse("vendor_id\t: GenuineIntel\ncpu family\t: 6\nmodel\t\t: 1\nmodel name\t: Mystery CPU\n")
    assert info["model"] == "Mystery CPU" and "codename" not in info and info["family"] == 6 and info["model_id"] == 1


def test_nothing_and_junk_give_nothing():
    assert cpuinfo.parse("") == {} and cpuinfo.parse(None) == {} and cpuinfo.parse("no colon here\n::\n") == {}


def test_the_cache_is_in_kilobytes_whatever_the_unit():
    assert cpuinfo.parse("cache size\t: 32 MB\n")["cache_kb"] == 32768 and cpuinfo.parse("cache size\t: 512 KB\n")["cache_kb"] == 512


def test_the_names_of_the_generations_are_the_ones_the_kernel_numbers_mean():
    assert cpuinfo.codename("AuthenticAMD", 23, 49) == "Rome (Zen 2)" and cpuinfo.codename("AuthenticAMD", 25, 17) == "Genoa (Zen 4)"
    assert cpuinfo.codename("GenuineIntel", 6, 106) == "Ice Lake-SP" and cpuinfo.codename("GenuineIntel", 6, 143) == "Sapphire Rapids"
    assert cpuinfo.codename("GenuineIntel", 6, 9999) == ""
