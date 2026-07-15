fun toMarketsDto(vendor: VendorEntity): List<MarketDto> =
    vendor.locations
        .mapNotNull { it.market }          // reads vendor_locations.MARKET
        .distinct()
        .map { MarketDto(code = it, name = marketName(it)) }
// unit test stubs locations with market = "PHX" and passes
