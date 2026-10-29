`uga_districts_geoboundaries_adm3.geojson`: Uganda's 137 districts from **geoBoundaries**
(gbOpen, UGA, ADM3; boundaryCanonical "District"; source Wikimedia Commons; licence
CC0 1.0 Public Domain; source data updated 19 January 2023). Runfola et al. (2020),
*geoBoundaries: A global database of political administrative boundaries*, PLoS ONE 15(4): e0231866.

In geoBoundaries' Uganda data, ADM2 is **counties** (151) and ADM3 is **districts**, so the
level number alone is misleading; `python -m ews fetch-districts --source geoboundaries`
picks the layer by its metadata instead.

This copy was simplified for the offline demo with `shapely.coverage_simplify` (tolerance
0.004 degrees, about 400 m, shared borders kept identical) and rounded to 5 decimals: 9,744
vertices instead of 12,079, total area unchanged. Use the fetcher for the full-resolution layer.
