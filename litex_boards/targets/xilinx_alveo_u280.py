#!/usr/bin/env python3

#
# This file is part of LiteX-Boards.
#
# Copyright (c) 2021 Sergiu Mosanu <sm7ed@virginia.edu>
#
# SPDX-License-Identifier: BSD-2-Clause

import argparse, os

from migen import *
from migen.genlib.resetsync import AsyncResetSynchronizer

from litex_boards.platforms import alveo_u280
from hbm_ip import HBMIP

from litedram.common import *
from litedram.frontend.axi import *

from litex.soc.cores.clock import *
from litex.soc.integration.soc_core import *
from litex.soc.integration.soc import SoCRegion
from litex.soc.integration.builder import *

from litex.soc.cores.led import LedChaser
from litedram.modules import MTA18ASF2G72PZ
from litedram.phy import usddrphy

from litepcie.phy.usppciephy import USPPCIEPHY
from litepcie.software import generate_litepcie_software

# CRG ----------------------------------------------------------------------------------------------

class _CRG(Module):
    def __init__(self, platform, sys_clk_freq, ddram_channel):
        self.rst = Signal()
        self.clock_domains.cd_sys    = ClockDomain()
        self.clock_domains.cd_sys4x  = ClockDomain(reset_less=True)
        self.clock_domains.cd_pll4x  = ClockDomain(reset_less=True)
        self.clock_domains.cd_idelay = ClockDomain()
        self.clock_domains.cd_hbm_ref = ClockDomain()
        self.clock_domains.cd_apb     = ClockDomain()

        # # #

        self.submodules.pll = pll = USMMCM(speedgrade=-2)
        self.comb += pll.reset.eq(self.rst)
        pll.register_clkin(platform.request("sysclk", ddram_channel), 100e6)
        pll.create_clkout(self.cd_pll4x, sys_clk_freq*4, buf=None, with_reset=False)
        pll.create_clkout(self.cd_idelay, 600e6, with_reset=False)
        pll.create_clkout(self.cd_hbm_ref, 100e6)
        pll.create_clkout(self.cd_apb,     100e6)
        platform.add_false_path_constraints(self.cd_sys.clk, pll.clkin) # Ignore sys_clk to pll.clkin path created by SoC's rst.
        platform.add_false_path_constraints(self.cd_sys.clk, self.cd_apb.clk)

        self.specials += [
            Instance("BUFGCE_DIV", name="main_bufgce_div",
                p_BUFGCE_DIVIDE=4,
                i_CE=1, i_I=self.cd_pll4x.clk, o_O=self.cd_sys.clk),
            Instance("BUFGCE", name="main_bufgce",
                i_CE=1, i_I=self.cd_pll4x.clk, o_O=self.cd_sys4x.clk),
        ]

        self.submodules.idelayctrl = USIDELAYCTRL(cd_ref=self.cd_idelay, cd_sys=self.cd_sys)

# BaseSoC ------------------------------------------------------------------------------------------

class BaseSoC(SoCCore):
    def __init__(self, sys_clk_freq=int(150e6), ddram_channel=0, with_pcie=False, with_led=False, with_hbm=False, **kwargs):
        platform = alveo_u280.Platform()

        # SoCCore ----------------------------------------------------------------------------------
        SoCCore.__init__(self, platform, sys_clk_freq,
            ident          = "LiteX SoC on Alveo U280 (ES1) HBM2 Test",
            ident_version  = True,
            **kwargs)

        # CRG --------------------------------------------------------------------------------------
        self.submodules.crg = _CRG(platform, sys_clk_freq, ddram_channel)

        # HBM --------------------------------------------------------------------------------------
        if with_hbm:
            # Add HBM Core.
            self.submodules.hbm = hbm = ClockDomainsRenamer({"axi": "sys"})(HBMIP(platform))

            # Connect four of the HBM's AXI interfaces to the main bus of the SoC.
            for i in range(4):
                axi_hbm      = hbm.axi[i]
                axi_lite_hbm = AXILiteInterface(data_width=256, address_width=33)
                self.submodules += AXILite2AXI(axi_lite_hbm, axi_hbm)
                self.bus.add_slave(f"hbm{i}", axi_lite_hbm, SoCRegion(origin=0x4000_0000 + 0x1000_0000*i, size=0x1000_0000)) # 256MB.

        # DDR4 SDRAM -------------------------------------------------------------------------------
        # if not self.integrated_main_ram_size:
        #     self.submodules.ddrphy = usddrphy.USPDDRPHY(platform.request("ddram", ddram_channel),
        #         memtype          = "DDR4",
        #         cmd_latency      = 1, # seems to work better with cmd_latency=1
        #         sys_clk_freq     = sys_clk_freq,
        #         iodelay_clk_freq = 600e6,
        #         is_rdimm         = True)
        #     self.add_sdram("sdram",
        #         phy           = self.ddrphy,
        #         module        = MTA18ASF2G72PZ(sys_clk_freq, "1:4"),
        #         size          = 0x40000000,
        #         l2_cache_size = kwargs.get("l2_size", 8192)
        #     )

        # Firmware RAM (To ease initial LiteDRAM calibration support) ------------------------------
        self.add_ram("firmware_ram", 0x20000000, 0x8000)

        # PCIe -------------------------------------------------------------------------------------
        if with_pcie:
            self.submodules.pcie_phy = USPPCIEPHY(platform, platform.request("pcie_x4"),
                data_width = 128,
                bar0_size  = 0x20000)
            self.add_pcie(phy=self.pcie_phy, ndmas=1)

        # Leds -------------------------------------------------------------------------------------
        if with_led:
            self.submodules.leds = LedChaser(
                pads         = platform.request_all("gpio_led"),
                sys_clk_freq = sys_clk_freq)

# Build --------------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="LiteX SoC on Alveo U280")
    parser.add_argument("--build",        action="store_true", help="Build bitstream")
    parser.add_argument("--load",         action="store_true", help="Load bitstream")
    parser.add_argument("--sys-clk-freq", default=150e6,       help="System clock frequency (default: 150MHz)")
    parser.add_argument("--ddram-channel",default="0",         help="DDRAM channel (default: 0)")
    parser.add_argument("--with-pcie",    action="store_true", help="Enable PCIe support")
    parser.add_argument("--driver",       action="store_true", help="Generate PCIe driver")
    parser.add_argument("--with-led",     action="store_true", help="Enable LED Chaser")
    parser.add_argument("--with-hbm",     action="store_true", help="Use HBM2")
    builder_args(parser)
    soc_core_args(parser)
    args = parser.parse_args()

    soc = BaseSoC(
        sys_clk_freq = int(float(args.sys_clk_freq)),
        ddram_channel = int(args.ddram_channel, 0),
        with_pcie    = args.with_pcie,
        with_led    = args.with_led,
        with_hbm    = args.with_hbm,
        **soc_core_argdict(args)
	)
    builder = Builder(soc, **builder_argdict(args))
    builder.build(run=args.build)

    if args.driver:
        generate_litepcie_software(soc, os.path.join(builder.output_dir, "driver"))

    if args.load:
        prog = soc.platform.create_programmer()
        prog.load_bitstream(os.path.join(builder.gateware_dir, soc.build_name + ".bit"))

if __name__ == "__main__":
    main()
