#!/usr/bin/env python3
"""Insert exact rendered source pages into generated blank mockups."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageOps


CANVAS_SIZE = (2000, 2000)
SOURCE_DIR = Path(
    "/Users/aaronnguyen/Developer/Etsy/working/2027-amethyst-google-calendar/source-review/rendered-final-144"
)


def scale_points(points: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    scale = CANVAS_SIZE[0] / 1254.0
    return [(x * scale, y * scale) for x, y in points]


def inset_points(points: list[tuple[float, float]], factor: float = 0.90) -> list[tuple[float, float]]:
    """Keep a clean white screen margin so the real page stays inside the bezel."""

    center_x = sum(point[0] for point in points) / len(points)
    center_y = sum(point[1] for point in points) / len(points)
    return [
        (center_x + (x - center_x) * factor, center_y + (y - center_y) * factor)
        for x, y in points
    ]


def solve_linear(matrix: list[list[float]], vector: list[float]) -> list[float]:
    """Solve a small dense system without adding a numerical dependency."""

    n = len(vector)
    augmented = [row[:] + [vector[index]] for index, row in enumerate(matrix)]
    for column in range(n):
        pivot = max(range(column, n), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            raise ValueError("singular homography system")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(n):
            if row == column:
                continue
            factor = augmented[row][column]
            if abs(factor) < 1e-15:
                continue
            augmented[row] = [
                left - factor * right
                for left, right in zip(augmented[row], augmented[column])
            ]
    return [augmented[index][-1] for index in range(n)]


def matrix_inverse(matrix: list[list[float]]) -> list[list[float]]:
    size = len(matrix)
    augmented = [
        row[:] + [1.0 if row_index == column_index else 0.0 for column_index in range(size)]
        for row_index, row in enumerate(matrix)
    ]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            raise ValueError("singular matrix")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            if abs(factor) < 1e-15:
                continue
            augmented[row] = [
                left - factor * right
                for left, right in zip(augmented[row], augmented[column])
            ]
    return [row[size:] for row in augmented]


def homography(src_points: list[tuple[float, float]], dst_points: list[tuple[float, float]]) -> list[list[float]]:
    """Return H such that dst ~= H * src, with H[2][2] normalized to one."""

    matrix: list[list[float]] = []
    vector: list[float] = []
    for (source_x, source_y), (destination_x, destination_y) in zip(src_points, dst_points):
        matrix.append([
            source_x,
            source_y,
            1.0,
            0.0,
            0.0,
            0.0,
            -destination_x * source_x,
            -destination_x * source_y,
        ])
        vector.append(destination_x)
        matrix.append([
            0.0,
            0.0,
            0.0,
            source_x,
            source_y,
            1.0,
            -destination_y * source_x,
            -destination_y * source_y,
        ])
        vector.append(destination_y)
    values = solve_linear(matrix, vector)
    return [
        [values[0], values[1], values[2]],
        [values[3], values[4], values[5]],
        [values[6], values[7], 1.0],
    ]


def warp_into_canvas(page: Image.Image, destination: list[tuple[float, float]]) -> Image.Image:
    source_width, source_height = page.size
    source_points = [
        (0.0, 0.0),
        (float(source_width - 1), 0.0),
        (float(source_width - 1), float(source_height - 1)),
        (0.0, float(source_height - 1)),
    ]
    forward = homography(source_points, destination)
    inverse = matrix_inverse(forward)
    coefficients = (
        inverse[0][0],
        inverse[0][1],
        inverse[0][2],
        inverse[1][0],
        inverse[1][1],
        inverse[1][2],
        inverse[2][0],
        inverse[2][1],
    )
    return page.transform(
        CANVAS_SIZE,
        Image.Transform.PERSPECTIVE,
        coefficients,
        resample=Image.Resampling.BICUBIC,
        fillcolor=(0, 0, 0, 0),
    )


def page_path(number: int) -> Path:
    return SOURCE_DIR / f"page-{number:03d}.png"


# Coordinates are measured against the 1254x1254 Image Generation output and
# point to the inner white display area, in clockwise order from top-left.
PLACEMENTS: dict[str, dict[str, list[tuple[int, list[tuple[float, float]]]]]] = {
    "temply": {
        "01": [(21, [(209, 348), (1026, 335), (1075, 889), (301, 974)])],
        "02": [
            (2, [(140, 151), (638, 151), (638, 529), (140, 529)]),
            (21, [(718, 407), (1184, 407), (1184, 850), (718, 850)]),
        ],
        "03": [
            (21, [(349, 206), (888, 188), (905, 765), (361, 787)]),
            (35, [(80, 727), (556, 651), (604, 1024), (110, 1064)]),
            (740, [(738, 638), (1206, 676), (1160, 967), (712, 944)]),
        ],
        "04": [(21, [(174, 213), (1027, 126), (1086, 881), (168, 980)])],
        "05": [(35, [(279, 346), (989, 410), (1008, 887), (184, 855)])],
        "06": [(29, [(355, 411), (1080, 484), (1048, 976), (303, 902)])],
        "07": [
            (30, [(82, 489), (553, 414), (590, 868), (118, 922)]),
            (35, [(568, 449), (1119, 416), (1163, 837), (600, 883)]),
        ],
        "08": [(2, [(142, 419), (886, 288), (1008, 903), (200, 1000)])],
        "09": [(740, [(283, 483), (1003, 480), (1021, 966), (270, 965)])],
        "10": [(1, [(132, 340), (877, 223), (1022, 766), (251, 873)])],
    },
    "daisy": {
        "01": [(21, [(260, 343), (1045, 342), (1075, 963), (250, 965)])],
        "02": [
            (21, [(145, 218), (573, 178), (632, 564), (176, 614)]),
            (35, [(92, 671), (575, 604), (622, 1015), (145, 1062)]),
            (28, [(621, 648), (1088, 644), (1128, 1015), (652, 1015)]),
        ],
        "03": [
            (21, [(174, 161), (735, 88), (782, 455), (212, 516)]),
            (30, [(572, 501), (1104, 472), (1148, 748), (598, 811)]),
            (35, [(214, 857), (769, 858), (770, 1127), (218, 1124)]),
        ],
        "04": [(21, [(275, 309), (1087, 414), (1038, 904), (170, 816)])],
        "05": [(35, [(201, 340), (951, 288), (1026, 877), (275, 941)])],
        "06": [(29, [(257, 416), (966, 344), (1034, 863), (281, 925)])],
        "07": [
            (30, [(152, 175), (781, 107), (826, 570), (192, 616)]),
            (35, [(446, 602), (1073, 631), (1069, 1045), (424, 1017)]),
        ],
        "08": [(2, [(262, 580), (1011, 579), (1025, 1117), (260, 1117)])],
        "09": [(740, [(280, 441), (995, 376), (1040, 938), (305, 1008)])],
        "10": [(1, [(260, 253), (959, 151), (1029, 707), (312, 774)])],
    },
}


def build_shop(shop: str, generated_dir: Path, draft_dir: Path) -> None:
    draft_dir.mkdir(parents=True, exist_ok=True)
    for slot in [f"{index:02d}" for index in range(1, 11)]:
        generated_path = generated_dir / f"{slot}.png"
        if not generated_path.is_file():
            raise FileNotFoundError(generated_path)
        with Image.open(generated_path) as opened:
            canvas = ImageOps.exif_transpose(opened).convert("RGB").resize(CANVAS_SIZE, Image.Resampling.LANCZOS).convert("RGBA")
        for page_number, points in PLACEMENTS[shop][slot]:
            source = page_path(page_number)
            if not source.is_file():
                raise FileNotFoundError(source)
            with Image.open(source) as opened_page:
                page = opened_page.convert("RGBA")
            warped = warp_into_canvas(page, scale_points(inset_points(points)))
            canvas = Image.alpha_composite(canvas, warped)
        output = draft_dir / f"{slot}.png"
        canvas.convert("RGB").save(output, format="PNG", optimize=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shop", choices=("temply", "daisy"), required=True)
    parser.add_argument("--generated-dir", type=Path, required=True)
    parser.add_argument("--draft-dir", type=Path, required=True)
    args = parser.parse_args()
    build_shop(args.shop, args.generated_dir, args.draft_dir)
    print(f"built {args.shop} drafts in {args.draft_dir}")


if __name__ == "__main__":
    main()
