#!/usr/bin/env python3
"""Apply after igc_trace_patch.py and igc_poll_patch.py (instead of
igc_wait_patch.py, which panicked: its retries and writes stalled the
chipset switch the Lexar NVMe shares with the I225-V).
Read-only timeline: at the first start(), schedule 4 samples at +5/+15/+30/+60 s.
Each sample reads the I225-V's vendor ID through pdev and through the parent
bridge, plus DP50's own vendor ID (sanity check), Link Control/Status and Slot
Control/Status (one 32-bit read each). 1.9d82 adds DP50-only reads: bus numbers
(0x18), secondary status (0x1C), bridge control (0x3C), device control/status
(PCIe cap + 0x08) and AER uncorrectable/correctable status (AER + 0x04 / 0x10).
1.9d83 adds the same kind of reads on UP00 (switch upstream port) and GPP7
(root port), plus GPP7's Root Control/Capabilities/Status and root AER error
status.
The PCIe capability offset comes from findPCICapability()'s out parameter
(1.9d80 used its return value by mistake, which is the capability register).
No writes, no loops. The samples run from a thread_call, so they keep going
after start() fails, and land in AppleIGC-trace."""
import pathlib, sys

SAMPLES_S = [5, 15, 30, 60]

def patch(path, old, new, count=1):
    p = pathlib.Path(path)
    s = p.read_text()
    n = s.count(old)
    if n != count:
        sys.exit(f"{path}: expected {count} match(es), found {n}:\n{old}")
    p.write_text(s.replace(old, new))
    print(f"patched {path}")

# 1. Headers and a forward declaration.
patch("AppleIGC/AppleIGC.cpp",
      '#include "AppleIGC.hpp"\n',
      '#include "AppleIGC.hpp"\n#include <IOKit/pci/IOPCIBridge.h>\n'
      '#include <kern/thread_call.h>\n'
      '#include <kern/clock.h>\n'
      '#undef ALIGN  // the IOKit header redefines it; restore the kcompat.h one\n'
      '#define ALIGN(x,a) (((x)+(a)-1)&~((a)-1))\n'
      'static void igc_start_sampler(IOPCIDevice *pdev);\n')

# 2. Start the sampler once, right after the cfg line in start().
patch("AppleIGC/AppleIGC.cpp",
      '    pr_err("start() [trace build, polling fallback]\\n");\n',
      '    pr_err("start() [trace build, polling fallback]\\n");\n'
      '    igc_start_sampler(OSDynamicCast(IOPCIDevice, provider));\n')

# 3. The sampler (appended after igc_trace()).
delays = ", ".join(str(d) for d in SAMPLES_S)
p = pathlib.Path("AppleIGC/AppleIGC.cpp")
s = p.read_text()
p.write_text(s + r'''
static const UInt32 kIgcSampleS[] = { ''' + delays + r''' };
static IOPCIDevice *gSampDev, *gSampDP;
static IOPCIBridge *gSampBr;
static UInt32 gSampCap;
static IOByteCount gSampAer;
static IOPCIDevice *gSampUp, *gSampRp;  // UP00 (switch upstream port) and GPP7 (root port)

// One line per upstream bridge: bus numbers, secondary status, link, device status,
// root control/capabilities/status (root port only) and AER status. Reads only.
static void igc_dump_bridge(const char *name, IOPCIDevice *d, bool root) {
    UInt8 cap = 0;
    IOByteCount aer = 0;
    d->findPCICapability(0x10, &cap);  // PCI Express capability ID
    d->extendedFindPCICapability(kIOPCIExpressErrorReportingCapability, &aer);
    UInt32 bus = d->configRead32(0x18), sec = d->configRead32(0x1c);
    UInt32 lnk = cap ? d->configRead32(cap + 0x10) : 0xdeadbeef;
    UInt32 dev = cap ? d->configRead32(cap + 0x08) : 0xdeadbeef;
    UInt32 ue = aer ? d->configRead32(aer + 0x04) : 0xdeadbeef;
    UInt32 ce = aer ? d->configRead32(aer + 0x10) : 0xdeadbeef;
    igc_trace(PFX "  %s bus %u/%u/%u | secsta 0x%04x | lnkctl 0x%04x lnksta 0x%04x | devsta 0x%04x | aer 0x%x ue 0x%08x ce 0x%08x\n",
              name, bus & 0xff, (bus >> 8) & 0xff, (bus >> 16) & 0xff, sec >> 16,
              lnk & 0xffff, lnk >> 16, dev >> 16, (unsigned)aer, ue, ce);
    if (root && cap) {
        UInt32 rc = d->configRead32(cap + 0x1c), rs = d->configRead32(cap + 0x20);
        UInt32 res = aer ? d->configRead32(aer + 0x30) : 0xdeadbeef;
        igc_trace(PFX "  %s rootctl 0x%04x rootcap 0x%04x rootsta 0x%08x | aer rootsta 0x%08x\n",
                  name, rc & 0xffff, rc >> 16, rs, res);
    }
}
static uint64_t gSampT0;
static thread_call_t gSampCall;

static void igc_sample(thread_call_param_t, thread_call_param_t p1) {
    uintptr_t i = (uintptr_t)p1;
    uint64_t ns;
    absolutetime_to_nanoseconds(mach_absolute_time() - gSampT0, &ns);
    UInt32 id  = gSampDev->configRead32(kIOPCIConfigVendorID);
    UInt32 bid = gSampBr ? gSampBr->configRead32(gSampDev->space, kIOPCIConfigVendorID) : 0xdeadbeef;
    UInt32 dpid = gSampDP ? gSampDP->configRead32(kIOPCIConfigVendorID) : 0xdeadbeef;
    UInt32 lnk = (gSampDP && gSampCap) ? gSampDP->configRead32(gSampCap + 0x10) : 0xdeadbeef;
    UInt32 slt = (gSampDP && gSampCap) ? gSampDP->configRead32(gSampCap + 0x18) : 0xdeadbeef;
    igc_trace(PFX "sample %lu +%llu ms: id 0x%08x via bridge 0x%08x inactive %d | DP50 id 0x%08x lnkctl 0x%04x lnksta 0x%04x sltctl 0x%04x sltsta 0x%04x\n",
           (unsigned long)i + 1, ns / 1000000ULL, id, bid, gSampDev->isInactive(), dpid,
           lnk & 0xffff, lnk >> 16, slt & 0xffff, slt >> 16);
    if (gSampDP) {  // DP50 only: bus numbers, secondary status, bridge control, device status, AER
        UInt32 bus = gSampDP->configRead32(0x18);
        UInt32 sec = gSampDP->configRead32(0x1c);
        UInt32 bctl = gSampDP->configRead32(0x3c);
        UInt32 dev = gSampCap ? gSampDP->configRead32(gSampCap + 0x08) : 0xdeadbeef;
        UInt32 ue = gSampAer ? gSampDP->configRead32(gSampAer + 0x04) : 0xdeadbeef;
        UInt32 ce = gSampAer ? gSampDP->configRead32(gSampAer + 0x10) : 0xdeadbeef;
        igc_trace(PFX "  DP50 bus pri %u sec %u sub %u | secsta 0x%04x | brctl 0x%04x | devctl 0x%04x devsta 0x%04x | aer ue 0x%08x ce 0x%08x\n",
               bus & 0xff, (bus >> 8) & 0xff, (bus >> 16) & 0xff, sec >> 16, bctl >> 16,
               dev & 0xffff, dev >> 16, ue, ce);
    }
    if (gSampUp) igc_dump_bridge("UP00", gSampUp, false);
    if (gSampRp) igc_dump_bridge("GPP7", gSampRp, true);
    gSampDev->setProperty("AppleIGC-trace", gIgcTraceBuf);  // start() may have cleared the provider
    if (++i < sizeof(kIgcSampleS) / sizeof(kIgcSampleS[0])) {
        uint64_t want;  // deadlines are relative to t0, not to this sample
        nanoseconds_to_absolutetime((uint64_t)kIgcSampleS[i] * 1000000000ULL, &want);
        thread_call_enter1_delayed(gSampCall, (thread_call_param_t)i, gSampT0 + want);
    } else {
        igc_trace(PFX "sampler done\n");
        gSampDev->setProperty("AppleIGC-trace", gIgcTraceBuf);
    }
}

static void igc_start_sampler(IOPCIDevice *pdev) {
    if (gSampCall || !pdev)
        return;  // start() runs twice; sample once
    gSampDev = pdev;
    gSampDev->retain();
    gSampBr = OSDynamicCast(IOPCIBridge, pdev->getProvider());
    if (gSampBr) {
        gSampBr->retain();
        gSampDP = OSDynamicCast(IOPCIDevice, gSampBr->getProvider());
        if (gSampDP) {
            gSampDP->retain();
            UInt8 off = 0;  // the return value is the capability register, the offset comes back here
            if (gSampDP->findPCICapability(0x10, &off))  // PCI Express capability ID
                gSampCap = off;
            IOByteCount aer = 0;
            if (gSampDP->extendedFindPCICapability(kIOPCIExpressErrorReportingCapability, &aer))
                gSampAer = aer;
        }
    }
    if (gSampDP) {  // DP50 -> IOPP -> UP00 -> IOPP -> GPP7
        IOService *b = gSampDP->getProvider();
        gSampUp = b ? OSDynamicCast(IOPCIDevice, b->getProvider()) : NULL;
        b = gSampUp ? gSampUp->getProvider() : NULL;
        gSampRp = b ? OSDynamicCast(IOPCIDevice, b->getProvider()) : NULL;
        if (gSampUp) gSampUp->retain();
        if (gSampRp) gSampRp->retain();
    }
    igc_trace(PFX "sampler: bridge %s, DP %s, DP PCIe cap 0x%x, AER 0x%x, up %s, root %s, samples at +%u/+%u/+%u/+%u s\n",
           gSampBr ? gSampBr->getName() : "none", gSampDP ? gSampDP->getName() : "none",
           (unsigned)gSampCap, (unsigned)gSampAer,
           gSampUp ? gSampUp->getName() : "none", gSampRp ? gSampRp->getName() : "none", kIgcSampleS[0], kIgcSampleS[1], kIgcSampleS[2], kIgcSampleS[3]);
    gSampT0 = mach_absolute_time();
    gSampCall = thread_call_allocate(igc_sample, NULL);
    uint64_t deadline;
    nanoseconds_to_absolutetime((uint64_t)kIgcSampleS[0] * 1000000000ULL, &deadline);
    thread_call_enter1_delayed(gSampCall, (thread_call_param_t)0, gSampT0 + deadline);
}
''')
print("appended igc_sample()")
