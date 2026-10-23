#!/usr/bin/env python3
"""Polling fallback for AppleIGC. On this AM5 board macOS never reads the
I225-V's capability list, so the card gets no interrupt (no MSI, no INTx)
and IOInterruptEventSource creation fails. Instead of failing start(), run
the interrupt handler from a timer every POLL_US microseconds.
Run from the repository root, after igc_trace_patch.py."""
import pathlib, sys

POLL_US = 250  # 64 packets per tick (igc_poll budget) ~ 3 Gbit/s at 1500 B

def patch(path, old, new, count=1):
    p = pathlib.Path(path)
    s = p.read_text()
    n = s.count(old)
    if n != count:
        sys.exit(f"{path}: expected {count} match(es), found {n}:\n{old}")
    p.write_text(s.replace(old, new))
    print(f"patched {path}")

# 1. Members and the timer callback.
patch("AppleIGC/AppleIGC.hpp",
      "    IOInterruptEventSource * interruptSource;\n",
      "    IOInterruptEventSource * interruptSource;\n"
      "    IOTimerEventSource * pollSource;\n"
      "    bool pollRunning;\n")
patch("AppleIGC/AppleIGC.hpp",
      "    static void interruptHandler(OSObject * target,\n",
      "    static void pollHandler(OSObject * target, IOTimerEventSource * src);\n"
      "    void setInterrupts(bool on);\n"
      "    static void interruptHandler(OSObject * target,\n")

# 2. init(): start without a poll timer.
patch("AppleIGC/AppleIGC.cpp",
      "    interruptSource = NULL;\n",
      "    interruptSource = NULL;\n    pollSource = NULL;\n    pollRunning = false;\n")

# 3. initEventSources(): fall back to the poll timer instead of failing.
patch("AppleIGC/AppleIGC.cpp",
      "    interruptSource = IOInterruptEventSource::interruptEventSource(this,&AppleIGC::interruptHandler,provider,interruptIndex);\n"
      "    if (!interruptSource) {\n"
      "        pr_err(\"MSI interrupt could not be enabled.\\n\");\n"
      "        goto error1;\n"
      "    }\n"
      "    myWorkLoop->addEventSource(interruptSource);\n",
      "    interruptSource = IOInterruptEventSource::interruptEventSource(this,&AppleIGC::interruptHandler,provider,interruptIndex);\n"
      "    if (interruptSource) {\n"
      "        myWorkLoop->addEventSource(interruptSource);\n"
      "    } else {\n"
      "        // ponytail: no interrupt from macOS on this board; poll instead (costs a 4 kHz timer)\n"
      "        pr_err(\"No interrupt available, using %d us polling\\n\", " + str(POLL_US) + ");\n"
      "        pollSource = IOTimerEventSource::timerEventSource(this, &AppleIGC::pollHandler);\n"
      "        if (!pollSource) {\n"
      "            pr_err(\"MSI interrupt could not be enabled.\\n\");\n"
      "            goto error1;\n"
      "        }\n"
      "        myWorkLoop->addEventSource(pollSource);\n"
      "        usingMsi = true; // process every ICR read, there's no shared line to filter\n"
      "    }\n")
patch("AppleIGC/AppleIGC.cpp",
      "error2:\n    workLoop->removeEventSource(interruptSource);\n    RELEASE(interruptSource);\n",
      "error2:\n    if (interruptSource) {\n        workLoop->removeEventSource(interruptSource);\n        RELEASE(interruptSource);\n    }\n"
      "    if (pollSource) {\n        workLoop->removeEventSource(pollSource);\n        RELEASE(pollSource);\n    }\n")

# 4. enable/disable paths and teardown go through setInterrupts().
# the 8-space one first: it also contains the 4-space text
patch("AppleIGC/AppleIGC.cpp", "        interruptSource->enable();\n", "        setInterrupts(true);\n")
patch("AppleIGC/AppleIGC.cpp", "    interruptSource->enable();\n", "    setInterrupts(true);\n")
patch("AppleIGC/AppleIGC.cpp", "        interruptSource->disable();\n", "        setInterrupts(false);\n")
patch("AppleIGC/AppleIGC.cpp",
      "        if (interruptSource) {\n"
      "            workLoop->removeEventSource(interruptSource);\n"
      "            RELEASE(interruptSource);\n"
      "        }\n",
      "        if (interruptSource) {\n"
      "            workLoop->removeEventSource(interruptSource);\n"
      "            RELEASE(interruptSource);\n"
      "        }\n"
      "        if (pollSource) {\n"
      "            pollRunning = false;\n"
      "            pollSource->cancelTimeout();\n"
      "            workLoop->removeEventSource(pollSource);\n"
      "            RELEASE(pollSource);\n"
      "        }\n")

# 5. The helpers, next to interruptHandler().
patch("AppleIGC/AppleIGC.cpp",
      "void AppleIGC::interruptHandler(OSObject * target, IOInterruptEventSource * src, int count)\n",
      "void AppleIGC::setInterrupts(bool on)\n{\n"
      "    if (interruptSource) {\n"
      "        if (on) interruptSource->enable(); else interruptSource->disable();\n"
      "        return;\n"
      "    }\n"
      "    pollRunning = on;\n"
      "    if (on) pollSource->setTimeoutUS(" + str(POLL_US) + "); else pollSource->cancelTimeout();\n"
      "}\n\n"
      "void AppleIGC::pollHandler(OSObject * target, IOTimerEventSource * src)\n{\n"
      "    AppleIGC * me = (AppleIGC *) target;\n"
      "    if (!me->pollRunning) return;\n"
      "    if (me->enabledForNetif) me->interruptOccurred(NULL, 0);\n"
      "    src->setTimeoutUS(" + str(POLL_US) + ");\n"
      "}\n\n"
      "void AppleIGC::interruptHandler(OSObject * target, IOInterruptEventSource * src, int count)\n")

# 6. Trace what config space looks like by the time start() runs.
patch("AppleIGC/AppleIGC.cpp",
      '    pr_err("start() [trace build]\\n");\n',
      '    pr_err("start() [trace build, polling fallback]\\n");\n'
      '    {\n        IOPCIDevice *d = OSDynamicCast(IOPCIDevice, provider);\n'
      '        if (d) pr_err("cfg id 0x%08x cmd 0x%04x cap ptr 0x%02x int pin %u line %u\\n",'
      ' d->configRead32(0), d->configRead16(4), d->configRead8(0x34), d->configRead8(0x3d), d->configRead8(0x3c));\n    }\n')
