#!/usr/bin/env python3
"""Apply after igc_trace_patch.py (and igc_poll_patch.py).
At start(), the I225-V reads all-ones (config + MMIO) although the link is
up later in the boot. This waits up to 30 s for the device to answer, then
restores BAR0/BAR3 and the command register (they're cleared if the device
was reset by a link drop), logging everything to AppleIGC-trace.

pdev->configRead32() also returns all-ones when IOPCIFamily's configAccess()
refuses the access (device inactive, parent in reset, configProt), and drops
writes the same way. So every read is also done through the parent bridge
(IOPCIBridge::configRead32(space, ...)), which skips that check; if only the
bridge path answers, the restore writes go through the bridge too."""
import pathlib, sys

p = pathlib.Path("AppleIGC/AppleIGC.cpp")
s = p.read_text()
if "#include <IOKit/pci/IOPCIBridge.h>" not in s:
    s = s.replace('#include "AppleIGC.hpp"\n', '#include "AppleIGC.hpp"\n#include <IOKit/pci/IOPCIBridge.h>\n', 1)
anchor = '    if (pdev->open(this) == false) {\n        pr_err("pdev->open failed\\n");\n        return false;\n    }\n'
if s.count(anchor) != 1:
    sys.exit("anchor not found (apply igc_trace_patch.py first)")
s = s.replace(anchor, anchor + r'''
    {   /* wait for the device to answer config reads */
        IOPCIBridge *br = OSDynamicCast(IOPCIBridge, pdev->getProvider());
        UInt32 id = pdev->configRead32(kIOPCIConfigVendorID);
        UInt32 bid = br ? br->configRead32(pdev->space, kIOPCIConfigVendorID) : 0xdeadbeef;
        pr_err("wait: start id 0x%08x, via bridge 0x%08x (bridge %s), inactive %d\n",
               id, bid, br ? br->getName() : "none", pdev->isInactive());
        int waited = 0;
        while (id == 0xffffffff && bid == 0xffffffff && waited < 30000) {
            IOSleep(100);
            waited += 100;
            id = pdev->configRead32(kIOPCIConfigVendorID);
            bid = br->configRead32(pdev->space, kIOPCIConfigVendorID);
        }
        pr_err("wait: id 0x%08x, via bridge 0x%08x, after %d ms\n", id, bid, waited);

        // configAccess() refused but the device answers: talk to it through the bridge
        bool direct = (id == 0xffffffff && br && bid != 0xffffffff);
        #define CR32(o) (direct ? br->configRead32(pdev->space, (o)) : pdev->configRead32(o))
        #define CR16(o) (direct ? br->configRead16(pdev->space, (o)) : pdev->configRead16(o))
        #define CW32(o, v) do { if (direct) br->configWrite32(pdev->space, (o), (v)); else pdev->configWrite32((o), (v)); } while (0)
        #define CW16(o, v) do { if (direct) br->configWrite16(pdev->space, (o), (v)); else pdev->configWrite16((o), (v)); } while (0)

        IODeviceMemory *m0 = pdev->getDeviceMemoryWithRegister(kIOPCIConfigBaseAddress0);
        IODeviceMemory *m3 = pdev->getDeviceMemoryWithRegister(kIOPCIConfigBaseAddress3);
        UInt32 bar0 = CR32(kIOPCIConfigBaseAddress0);
        UInt32 bar3 = CR32(kIOPCIConfigBaseAddress3);
        UInt16 cmd  = CR16(kIOPCIConfigCommand);
        pr_err("wait: %s bar0 0x%08x (want 0x%08llx) bar3 0x%08x (want 0x%08llx) cmd 0x%04x cap ptr 0x%02x\n",
               direct ? "[via bridge]" : "[normal]",
               bar0, m0 ? (unsigned long long)m0->getPhysicalAddress() : 0ULL,
               bar3, m3 ? (unsigned long long)m3->getPhysicalAddress() : 0ULL, cmd,
               CR32(0x34) & 0xff);
        if (id != 0xffffffff || direct) {
            if (m0 && (bar0 & ~0xfU) != (UInt32)m0->getPhysicalAddress())
                CW32(kIOPCIConfigBaseAddress0, (UInt32)m0->getPhysicalAddress());
            if (m3 && (bar3 & ~0xfU) != (UInt32)m3->getPhysicalAddress())
                CW32(kIOPCIConfigBaseAddress3, (UInt32)m3->getPhysicalAddress());
            CW16(kIOPCIConfigCommand,
                 (cmd & ~kIOPCICommandIOSpace) | kIOPCICommandMemorySpace | kIOPCICommandBusMaster);
            pr_err("wait: after restore bar0 0x%08x bar3 0x%08x cmd 0x%04x\n",
                   CR32(kIOPCIConfigBaseAddress0), CR32(kIOPCIConfigBaseAddress3), CR16(kIOPCIConfigCommand));
        }
        #undef CR32
        #undef CR16
        #undef CW32
        #undef CW16
    }
''')
p.write_text(s)
print("patched AppleIGC.cpp (wait + BAR restore, bridge-direct fallback)")
