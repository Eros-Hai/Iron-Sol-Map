#!/usr/bin/env python3
"""Build the browser-ready Iron Sols map from the Tiled source files.

Usage:
    python3 tools/build_map.py \
        "/path/to/Iron Sol map" \
        "/path/to/iron-sol-map-site"

The output intentionally uses the individual screenshot tiles instead of the
giant composite Map.png. The browser viewer streams those tiles as the user
zooms and pans.
"""

from __future__ import annotations

import argparse
import json
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image


# The overview remains mounted behind streamed tiles as a visual fallback, so
# keep it modest enough to stay cheap in browser memory.
THUMB_WIDTH = 80
THUMB_HEIGHT = 45
MID_WIDTH = 1440
MID_HEIGHT = 810


def parse_csv_chunk(chunk: ET.Element) -> list[int]:
    data = chunk.text or ""
    return [int(value.strip()) for value in data.replace("\n", ",").split(",") if value.strip()]


def build(source_root: Path, output_root: Path) -> dict:
    tmx_path = source_root / "Map.tmx"
    tsx_path = source_root / "Images" / "Iron Sol Screenshots.tsx"

    map_root = ET.parse(tmx_path).getroot()
    tileset_root = ET.parse(tsx_path).getroot()

    tile_w = int(map_root.attrib["tilewidth"])
    tile_h = int(map_root.attrib["tileheight"])
    first_gid = 1

    gid_to_image: dict[int, Path] = {}
    for tile in tileset_root.findall("tile"):
        tile_id = int(tile.attrib["id"])
        image = tile.find("image")
        if image is None or "source" not in image.attrib:
            continue
        gid_to_image[first_gid + tile_id] = tsx_path.parent / image.attrib["source"]

    layer = map_root.find("layer")
    if layer is None:
        raise ValueError("Map.tmx does not contain a tile layer")

    offset_x = float(layer.attrib.get("offsetx", 0))
    offset_y = float(layer.attrib.get("offsety", 0))
    data = layer.find("data")
    if data is None:
        raise ValueError("Map.tmx tile layer does not contain data")

    tiles: list[dict] = []
    used_gids: set[int] = set()
    chunk_bounds: list[tuple[int, int, int, int]] = []

    for chunk in data.findall("chunk"):
        chunk_x = int(chunk.attrib["x"])
        chunk_y = int(chunk.attrib["y"])
        chunk_width = int(chunk.attrib["width"])
        chunk_height = int(chunk.attrib["height"])
        values = parse_csv_chunk(chunk)
        if len(values) != chunk_width * chunk_height:
            raise ValueError(
                f"Chunk at {chunk_x},{chunk_y} has {len(values)} cells; "
                f"expected {chunk_width * chunk_height}"
            )

        chunk_bounds.append((chunk_x, chunk_y, chunk_width, chunk_height))

        for index, gid in enumerate(values):
            if gid == 0:
                continue
            if gid not in gid_to_image:
                raise ValueError(f"No tileset image found for GID {gid}")

            cell_x = chunk_x + index % chunk_width
            cell_y = chunk_y + index // chunk_width
            tiles.append(
                {
                    "gid": gid,
                    "cellX": cell_x,
                    "cellY": cell_y,
                    "x": offset_x + cell_x * tile_w,
                    "y": offset_y + cell_y * tile_h,
                }
            )
            used_gids.add(gid)

    if not chunk_bounds:
        raise ValueError("Map.tmx does not contain any chunks")

    # The chunk rectangle includes a large amount of empty space. Use the
    # occupied tile rectangle for the website's stage and overview so Fit Map
    # frames the actual map instead of fitting the unused 32x32 container.
    if not tiles:
        raise ValueError("Map.tmx does not contain any non-empty tiles")

    min_cell_x = min(tile["cellX"] for tile in tiles)
    min_cell_y = min(tile["cellY"] for tile in tiles)
    max_cell_x = max(tile["cellX"] for tile in tiles) + 1
    max_cell_y = max(tile["cellY"] for tile in tiles) + 1

    bounds = {
        "minX": offset_x + min_cell_x * tile_w,
        "minY": offset_y + min_cell_y * tile_h,
        "maxX": offset_x + max_cell_x * tile_w,
        "maxY": offset_y + max_cell_y * tile_h,
        "width": (max_cell_x - min_cell_x) * tile_w,
        "height": (max_cell_y - min_cell_y) * tile_h,
        "minCellX": min_cell_x,
        "minCellY": min_cell_y,
        "maxCellX": max_cell_x,
        "maxCellY": max_cell_y,
    }

    output_root.mkdir(parents=True, exist_ok=True)
    tiles_output = output_root / "assets" / "tiles"
    tiles_output.mkdir(parents=True, exist_ok=True)
    mid_tiles_output = output_root / "assets" / "screen-tiles"
    mid_tiles_output.mkdir(parents=True, exist_ok=True)

    image_name_by_gid: dict[int, str] = {}
    mid_image_name_by_gid: dict[int, str] = {}
    for gid in sorted(used_gids):
        source_image = gid_to_image[gid]
        if not source_image.exists():
            raise FileNotFoundError(source_image)
        target_name = f"gid-{gid}.png"
        shutil.copy2(source_image, tiles_output / target_name)
        image_name_by_gid[gid] = f"assets/tiles/{target_name}"

        mid_target_name = f"gid-{gid}.webp"
        with Image.open(source_image) as full_image:
            mid_image = full_image.convert("RGBA").resize(
                (MID_WIDTH, MID_HEIGHT), Image.Resampling.LANCZOS
            )
            mid_image.save(
                mid_tiles_output / mid_target_name,
                "WEBP",
                quality=92,
                method=6,
            )
        mid_image_name_by_gid[gid] = f"assets/screen-tiles/{mid_target_name}"

    for tile in tiles:
        tile["src"] = image_name_by_gid[tile["gid"]]
        tile["midSrc"] = mid_image_name_by_gid[tile["gid"]]

    # Build a low-resolution overview for the initial fit-to-map view. It keeps
    # the browser from decoding hundreds of full-resolution screenshots at once.
    overview_width = (max_cell_x - min_cell_x) * THUMB_WIDTH
    overview_height = (max_cell_y - min_cell_y) * THUMB_HEIGHT
    overview = Image.new("RGB", (overview_width, overview_height), (22, 27, 29))
    thumbnail_cache: dict[int, Image.Image] = {}

    for tile in tiles:
        gid = tile["gid"]
        if gid not in thumbnail_cache:
            with Image.open(gid_to_image[gid]) as source_image:
                thumbnail_cache[gid] = source_image.convert("RGB").resize(
                    (THUMB_WIDTH, THUMB_HEIGHT), Image.Resampling.LANCZOS
                )
        overview.paste(
            thumbnail_cache[gid],
            (
                (tile["cellX"] - min_cell_x) * THUMB_WIDTH,
                (tile["cellY"] - min_cell_y) * THUMB_HEIGHT,
            ),
        )

    overview_path = output_root / "assets" / "overview.jpg"
    overview.save(overview_path, "JPEG", quality=82, optimize=True, progressive=True)

    manifest = {
        "title": "Iron Sols Atlas",
        "tileWidth": tile_w,
        "tileHeight": tile_h,
        "overviewWidth": overview_width,
        "overviewHeight": overview_height,
        "offsetX": offset_x,
        "offsetY": offset_y,
        "bounds": bounds,
        "overview": "assets/overview.jpg",
        "tiles": tiles,
        "source": {
            "tileCount": len(tiles),
            "uniqueTileCount": len(used_gids),
            "chunkCount": len(chunk_bounds),
        },
    }

    data_output = output_root / "data"
    data_output.mkdir(parents=True, exist_ok=True)
    data_js = "window.IRON_SOLS_MAP = " + json.dumps(manifest, separators=(",", ":")) + ";\n"
    (data_output / "map-data.js").write_text(data_js, encoding="utf-8")

    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_root", type=Path)
    parser.add_argument("output_root", type=Path)
    args = parser.parse_args()
    manifest = build(args.source_root, args.output_root)
    print(
        f"Built {len(manifest['tiles'])} placed tiles, "
        f"{manifest['source']['uniqueTileCount']} unique images."
    )


if __name__ == "__main__":
    main()
