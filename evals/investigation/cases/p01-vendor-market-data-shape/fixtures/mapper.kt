fun toMarketsDto(vendor: VendorEntity): List<MarketDto> =
    vendor.locations
        .mapNotNull { it.market }          // reads supplier_sites.MARKET
        .distinct()
        .map { MarketDto(code = it, name = marketName(it)) }
// unit test stubs locations with market = "RGN-A" and passes
