# ADAC XML → ArcGIS Shapefile Converter

A Streamlit web app that converts ADAC v6 XML files to ArcGIS-compatible shapefiles.

## Features

- **Drag-and-drop** one or more ADAC XML files
- **Auto-detects CRS** from `<CoordinateSystem>` in the XML, or override with any GDA2020 / GDA94 MGA zone or WGS84
- **Filter geometry types** — export points, lines, polygons independently
- **Arc linearisation** — configurable segments per circular/elliptical arc
- **Handles all ADAC feature classes** — sewerage, stormwater, water, transport, open space, etc.
- **Mixed-type containers** (`EndStructures`, `WSUDPoints`) split automatically by geometry kind
- **Download** all shapefiles as a single ZIP

## Shapefile output structure

```
adac_shapefiles.zip
└── <XML filename stem>/
    ├── MaintenanceHoles.shp/.shx/.dbf/.prj
    ├── PipesNonPressure.shp/.shx/.dbf/.prj
    ├── EndStructures_point.shp/.shx/.dbf/.prj   ← mixed container
    ├── EndStructures_linear.shp/.shx/.dbf/.prj
    └── ...
```

## Installation

```bash
pip install -r requirements.txt
```

## Running locally

```bash
streamlit run app.py
```

## Deploying to Streamlit Community Cloud

1. Push this folder to a GitHub repository
2. Go to https://share.streamlit.io
3. Connect your repo and set `app.py` as the main file
4. Deploy — no extra secrets or configuration needed

## Dependencies

- `streamlit` — web UI
- `lxml` — XML parsing
- `pyshp` — shapefile writing
- `pandas` — results table

## Requirements

Python 3.9+
