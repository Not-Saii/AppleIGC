#!/usr/bin/env python3
"""Patch AppleIGC so every log line is also stored in the I225-V's ioreg
node as the property "AppleIGC-trace". IOLog output from AppleIGC never
shows up on this machine, but ioreg properties on the PCI device survive
even when start() fails. Run from the repository root."""
import pathlib, sys

def patch(path, old, new, count=1):
    p = pathlib.Path(path)
    s = p.read_text()
    n = s.count(old)
    if n != count:
        sys.exit(f"{path}: expected {count} match(es), found {n}:\n{old}")
    p.write_text(s.replace(old, new))
    print(f"patched {path}")

# 1. Route all IOLog calls in the driver through igc_trace().
patch("AppleIGC/igc/kcompat.h",
      '#define PFX "igc: "\n',
      '#define PFX "igc: "\n\n'
      '#ifdef IGC_TRACE\n'
      'void igc_trace(const char *fmt, ...) __attribute__((format(printf, 1, 2)));\n'
      '#define IOLog igc_trace\n'
      '#endif\n')

# 2. Forward declaration, then extra trace points in start() and igc_probe().
patch("AppleIGC/AppleIGC.cpp",
      '#include "AppleIGC.hpp"\n',
      '#include "AppleIGC.hpp"\n\n#ifdef IGC_TRACE\nextern IOService *gIgcTraceProvider;\n#endif\n')
patch("AppleIGC/AppleIGC.cpp",
      '    pr_err("start()\\n");\n',
      '#ifdef IGC_TRACE\n    gIgcTraceProvider = provider;\n#endif\n'
      '    pr_err("start() [trace build]\\n");\n')
patch("AppleIGC/AppleIGC.cpp",
      '    if (super::start(provider) == false) {\n        return false;\n',
      '    if (super::start(provider) == false) {\n        pr_err("super::start failed\\n");\n        return false;\n')
patch("AppleIGC/AppleIGC.cpp",
      '    if (pdev->open(this) == false)\n        return false;\n',
      '    if (pdev->open(this) == false) {\n        pr_err("pdev->open failed\\n");\n        return false;\n    }\n')
patch("AppleIGC/AppleIGC.cpp",
      '    if (csrPCIAddress == NULL) {\n        return false;\n',
      '    if (csrPCIAddress == NULL) {\n        pr_err("BAR0 map failed\\n");\n        return false;\n')
patch("AppleIGC/AppleIGC.cpp",
      '    hw->bus.pci_cmd_word = pdev->configRead16(kIOPCIConfigCommand);\n',
      '    hw->bus.pci_cmd_word = pdev->configRead16(kIOPCIConfigCommand);\n'
      '    pr_err("ids %04x:%04x sub %04x:%04x rev %02x cmd %04x STATUS 0x%08x\\n",'
      ' hw->vendor_id, hw->device_id, hw->subsystem_vendor_id, hw->subsystem_device_id,'
      ' hw->revision_id, hw->bus.pci_cmd_word, rd32(IGC_STATUS));\n')
patch("AppleIGC/AppleIGC.cpp",
      '    hw->mac.ops.reset_hw(hw);\n    \n    if (igc_get_flash_presence_i225(hw)) {\n'
      '        if (hw->nvm.ops.validate(hw) < 0) {\n',
      '    {\n        s32 rr = hw->mac.ops.reset_hw(hw);\n'
      '        pr_err("reset_hw=%d STATUS 0x%08x EECD 0x%08x\\n", rr, rd32(IGC_STATUS), rd32(IGC_EECD));\n    }\n'
      '    \n    if (igc_get_flash_presence_i225(hw)) {\n'
      '        pr_err("flash present\\n");\n'
      '        if (hw->nvm.ops.validate(hw) < 0) {\n')
patch("AppleIGC/AppleIGC.cpp",
      '    netif->registerService();\n\n    return true;\n}\n',
      '    netif->registerService();\n\n'
      '#ifdef IGC_TRACE\n    pr_err("start() succeeded\\n");\n    gIgcTraceProvider = NULL;\n#endif\n'
      '    return true;\n}\n')

# 3. The trace function itself (appended at the end of AppleIGC.cpp).
p = pathlib.Path("AppleIGC/AppleIGC.cpp")
s = p.read_text()
p.write_text(s + r'''
#ifdef IGC_TRACE
#undef IOLog
static char gIgcTraceBuf[16384];
static size_t gIgcTraceLen = 0;
IOService *gIgcTraceProvider = NULL;

extern "C" void igc_trace(const char *fmt, ...) {
    char line[256];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(line, sizeof(line), fmt, ap);
    va_end(ap);
    IOLog("%s", line);
    size_t n = strnlen(line, sizeof(line));
    if (gIgcTraceLen + n < sizeof(gIgcTraceBuf) - 1) {
        memcpy(gIgcTraceBuf + gIgcTraceLen, line, n);
        gIgcTraceLen += n;
        gIgcTraceBuf[gIgcTraceLen] = 0;
    }
    if (gIgcTraceProvider)
        gIgcTraceProvider->setProperty("AppleIGC-trace", gIgcTraceBuf);
}
#endif
''')
print("appended igc_trace()")
