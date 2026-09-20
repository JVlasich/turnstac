import pytest

try:  # bare interpreter: collection survives, the geo tests skip themselves
    from osgeo import gdal, osr
    gdal.UseExceptions()
except ImportError:
    gdal = osr = None


def _write_tif(path, value: int, size: int = 4, px: float = 25) -> None:
    """Small georeferenced GTiff (uncompressed = content change keeps the size)."""
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(31256)
    ds = gdal.GetDriverByName("GTiff").Create(str(path), size, size, 1, gdal.GDT_Byte)
    ds.SetGeoTransform((-53000, px, 0, 340000, 0, -px))
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).Fill(value)
    ds = None


def _write_masked_tif(path, value: int = 100) -> None:
    """64x64 on the _write_tif grid, nodata=0, only the left half holds data. Large enough that
    the valid half survives the footprint min-part filter (32x64 cells at 25 m = 1.28 km^2)."""
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(31256)
    ds = gdal.GetDriverByName("GTiff").Create(str(path), 64, 64, 1, gdal.GDT_Byte)
    ds.SetGeoTransform((-53000, 25, 0, 340000, 0, -25))
    ds.SetProjection(srs.ExportToWkt())
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(0)
    band.Fill(0)
    band.WriteRaster(0, 0, 32, 64, bytes([value]) * (32 * 64))
    ds = None


def _write_rgb_tif(path, size: int = 8) -> None:
    """3-band GTiff on the _write_tif grid. Colour interps make it an RGB image; the fills
    differ per band so the statistics do too (GTiff stores one nodata for the whole dataset,
    so that one is shared by construction)."""
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(31256)
    ds = gdal.GetDriverByName("GTiff").Create(str(path), size, size, 3, gdal.GDT_Byte)
    ds.SetGeoTransform((-53000, 25, 0, 340000, 0, -25))
    ds.SetProjection(srs.ExportToWkt())
    for i, ci in enumerate((gdal.GCI_RedBand, gdal.GCI_GreenBand, gdal.GCI_BlueBand), start=1):
        band = ds.GetRasterBand(i)
        band.SetColorInterpretation(ci)
        band.Fill(40 * i)
    ds.GetRasterBand(1).SetNoDataValue(0)
    ds = None


def _write_gradient_tif(path) -> None:
    """16x16 on the _write_tif grid holding every Byte value exactly once. Every other raster
    fixture is a constant fill, which has no value range to bin."""
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(31256)
    ds = gdal.GetDriverByName("GTiff").Create(str(path), 16, 16, 1, gdal.GDT_Byte)
    ds.SetGeoTransform((-53000, 25, 0, 340000, 0, -25))
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).WriteRaster(0, 0, 16, 16, bytes(range(256)))
    ds = None


def _write_tif_no_crs(path, size: int = 4) -> None:
    """Georeferenced grid but no CRS declared (legacy-file case)."""
    ds = gdal.GetDriverByName("GTiff").Create(str(path), size, size, 1, gdal.GDT_Byte)
    ds.SetGeoTransform((-53000, 25, 0, 340000, 0, -25))
    ds.GetRasterBand(1).Fill(10)
    ds = None


def _write_las(path, n=800, dx=0.0, dy=0.0, gps=None):
    """100 x 50 tile of random points offset by (dx, dy); the corners pin the extent exactly.
    gps = (min, max) writes a linear GPSTime ramp, otherwise the dimension stays constant
    (and extract drops it)."""
    import laspy
    import numpy as np
    rng = np.random.default_rng(0)
    x = dx + np.concatenate([rng.uniform(0, 100, n), [0.0, 100.0, 0.0, 100.0]])
    y = dy + np.concatenate([rng.uniform(0, 50, n), [0.0, 0.0, 50.0, 50.0]])
    z = np.concatenate([rng.uniform(0, 10, n), [0.0, 0.0, 0.0, 0.0]])
    las = laspy.LasData(laspy.LasHeader(point_format=3))
    las.x, las.y, las.z = x, y, z
    if gps:
        las.gps_time = np.linspace(gps[0], gps[1], len(x))
    las.write(str(path))


@pytest.fixture
def write_tif():
    return _write_tif


@pytest.fixture
def write_las():
    return _write_las


@pytest.fixture
def write_rgb_tif():
    return _write_rgb_tif


@pytest.fixture
def write_gradient_tif():
    return _write_gradient_tif


@pytest.fixture
def write_tif_no_crs():
    return _write_tif_no_crs


@pytest.fixture
def write_masked_tif():
    return _write_masked_tif
