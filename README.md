# Wayfare

![Coverage](docs/coverage.svg)

![Every service across Ireland and southern Britain, from Kerry to Lowestoft, drawn from the three published archives: road in blue through yellow by journeys a day, rail in orange, ferry in crimson](docs/banner.png)

Wayfare builds an aggregate dataset of public transport routes across Great Britain and Ireland. It produces a *PMTiles* archive, a tiled hierarchical map file, behind an interactive map where hovering a road lists the services on it.

## Quick start

Wales is 41 MB zipped and exercises every stage, which makes it a better first run than the national bundle. `WAYFARE_REGION` and `WAYFARE_OSM_URL` must describe the same area.

```console
$ cat > .env <<'EOF'
WAYFARE_REGION=wales
WAYFARE_OSM_URL=https://download.geofabrik.de/europe/united-kingdom/wales-latest.osm.pbf
EOF
$ direnv allow                      # or: nix develop
$ docker compose up -d valhalla     # builds its graph before it answers anything

$ wayfare acquire --region wales    # resolves its own extract URL from --region
$ wayfare patterns
$ wayfare match                     # the long one
$ wayfare aggregate
$ wayfare publish
$ wayfare status
$ wayfare serve                     # viewer on http://localhost:8099
```

## On a server

```console
$ docker compose up -d valhalla     # first start builds the graph
$ docker compose run --rm pipeline  # every stage, acquire through publish
$ docker compose up -d web          # viewer and renderer on :8099
```

`pipeline` runs `wayfare all`, which is safe to interrupt and re-run, since every stage skips work already done. It runs the same stages in the same order as [`deploy/refresh.sh`](deploy/refresh.sh), which is for incremental updates on a schedule.

## Data sources and licences

| Source | Covers | Licence |
|---|---|---|
| DfT Bus Open Data Service (BODS) GTFS | Great Britain | Open Government Licence (OGL) v3.0 |
| NaPTAN stop register | Great Britain | OGL v3.0 |
| National Transport Authority (NTA) GTFS, published as Transport for Ireland | Republic of Ireland | Creative Commons Attribution (CC BY) 4.0 |
| Translink TransXChange timetables and MapInfo route geometry, via OpenDataNI | Northern Ireland | OGL v3.0 |
| Geofabrik OpenStreetMap extracts | all three | Open Database License (ODbL) |

## Further reading

- [`docs/pipeline.md`](docs/pipeline.md) — the nine stages, storage, clustering, tiles.
- [`docs/data.md`](docs/data.md) — the feeds, their traps, mode filtering, coverage gaps, attribution.
- [`docs/deploy.md`](docs/deploy.md) — the scheduled refresh, and the publish gate that guards it.
