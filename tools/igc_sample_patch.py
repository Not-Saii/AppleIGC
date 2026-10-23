#!/usr/bin/env python3
"""Apply after igc_trace_patch.py and igc_poll_patch.py (instead of
igc_wait_patch.py, which panicked: its retries and writes stalled the
chipset switch the Lexar NVMe shares with the I225-V).
Read-only timeline: at the first start(), schedule 4 samples at +5/+15/+30/+60 s.
Each sample reads the I225-V's vendor ID through pdev and through the parent
bridge, plus DP50's Link Control/Status and Slot Control/Status (one 32-bit
read each). No writes, no loops. The samples run from a thread_call, so they
keep going after start() fails, and land in AppleIGC-trace."""
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
static uint64_t gSampT0;
static thread_call_t gSampCall;

static void igc_sample(thread_call_param_t, thread_call_param_t p1) {
    uintptr_t i = (uintptr_t)p1;
    uint64_t ns;
    absolutetime_to_nanoseconds(mach_absolute_time() - gSampT0, &ns);
    UInt32 id  = gSampDev->configRead32(kIOPCIConfigVendorID);
    UInt32 bid = gSampBr ? gSampBr->configRead32(gSampDev->space, kIOPCIConfigVendorID) : 0xdeadbeef;
    UInt32 lnk = (gSampDP && gSampCap) ? gSampDP->configRead32(gSampCap + 0x10) : 0xdeadbeef;
    UInt32 slt = (gSampDP && gSampCap) ? gSampDP->configRead32(gSampCap + 0x18) : 0xdeadbeef;
    igc_trace(PFX "sample %lu +%llu ms: id 0x%08x via bridge 0x%08x inactive %d | DP50 lnkctl 0x%04x lnksta 0x%04x sltctl 0x%04x sltsta 0x%04x\n",
           (unsigned long)i + 1, ns / 1000000ULL, id, bid, gSampDev->isInactive(),
           lnk & 0xffff, lnk >> 16, slt & 0xffff, slt >> 16);
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
            gSampCap = gSampDP->findPCICapability(0x10);  // PCI Express capability ID
        }
    }
    igc_trace(PFX "sampler: bridge %s, DP %s, DP PCIe cap 0x%x, samples at +%u/+%u/+%u/+%u s\n",
           gSampBr ? gSampBr->getName() : "none", gSampDP ? gSampDP->getName() : "none",
           (unsigned)gSampCap, kIgcSampleS[0], kIgcSampleS[1], kIgcSampleS[2], kIgcSampleS[3]);
    gSampT0 = mach_absolute_time();
    gSampCall = thread_call_allocate(igc_sample, NULL);
    uint64_t deadline;
    nanoseconds_to_absolutetime((uint64_t)kIgcSampleS[0] * 1000000000ULL, &deadline);
    thread_call_enter1_delayed(gSampCall, (thread_call_param_t)0, gSampT0 + deadline);
}
''')
print("appended igc_sample()")
