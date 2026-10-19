import ctypes
import os
import platform
import shutil
import string
import subprocess

def get_os_info():
    print("OS Name:      ", platform.system(), platform.release())
    print("OS Version:   ", platform.version())
    print("System Type:  ", platform.machine())


def get_cpu_name():
    """Marketing name of the CPU, e.g. 'AMD Ryzen 5 5600H with Radeon Graphics'."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
            return winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
    except Exception:
        return platform.processor()


def get_cpu_info():
    print("CPU Name:", get_cpu_name())
    print("Processor:              ", platform.processor())
    print("Logical cores (estimate):", os.cpu_count())  # stdlib count; may include hyperthreads
    try:
        # Physical vs logical core split needs psutil; fall back gracefully.
        import psutil  # optional, only if installed
        print("Physical cores (exact): ", psutil.cpu_count(logical=False))
        print("Logical cores (exact):  ", psutil.cpu_count(logical=True))
        freq = psutil.cpu_freq()
        if freq:
            print("Max Clock Speed (MHz):  ", round(freq.max))
    except ImportError:
        print("psutil: not installed (pip install psutil for exact core split and clock speed)")


def get_ram_info():
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    mem = MEMORYSTATUSEX()
    mem.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem))

    total_gb = mem.ullTotalPhys / (1024 ** 3)
    avail_gb = mem.ullAvailPhys / (1024 ** 3)
    print(f"Total RAM:     {mem.ullTotalPhys} bytes  (~{total_gb:.2f} GB)")
    print(f"Available RAM: {mem.ullAvailPhys} bytes  (~{avail_gb:.2f} GB)")
    print(f"Memory Load:   {mem.dwMemoryLoad}%")


def get_disk_info():
    letters = [d for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
    for letter in letters:
        drive = f"{letter}:\\"
        try:
            usage = shutil.disk_usage(drive)
            print(f"Drive {letter} FreeSpace (bytes): {usage.free}")
            print(f"Drive {letter} Size (bytes): {usage.total}")
        except (PermissionError, OSError):
            print(f"Drive {letter} Status: not accessible")


def get_gpu_info():
    # Try wmic first (works on your machine); fall back to PowerShell if missing.
    # Both paths print one "Label: Value" line per field per GPU -- never a raw
    # table -- so every field is a separate, cleanly parseable line.
    try:
        out = subprocess.check_output(
            ["wmic", "path", "win32_VideoController", "get",
             "Name", "/format:list"],
            stderr=subprocess.DEVNULL, text=True
        )
        gpu_index = 0
        current = {}
        for raw_line in out.splitlines() + [""]:  # trailing "" flushes the last block
            line = raw_line.strip()
            if not line:
                if current:
                    gpu_index += 1
                    for key, value in current.items():
                        print(f"GPU {gpu_index} {key}: {value}")
                    current = {}
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                current[key.strip()] = value.strip()
        if gpu_index == 0:
            raise RuntimeError("no GPU rows returned by wmic")
    except Exception:
        try:
            ps_cmd = (
                "Get-CimInstance Win32_VideoController | "
                "Select-Object Name | ConvertTo-Json"
            )
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                stderr=subprocess.DEVNULL, text=True
            )
            import json
            data = json.loads(out)
            if isinstance(data, dict):
                data = [data]
            for gpu_index, gpu in enumerate(data, start=1):
                for key, value in gpu.items():
                    print(f"GPU {gpu_index} {key}: {value}")
        except Exception as e:
            print("Could not retrieve GPU info:", e)


def get_architecture():
    print("Architecture:", platform.architecture()[0])
    print("Machine:     ", platform.machine())


def main():
    get_os_info()
    get_cpu_info()
    get_ram_info()
    get_disk_info()
    get_gpu_info()
    get_architecture()


if __name__ == "__main__":
    main()