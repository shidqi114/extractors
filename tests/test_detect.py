from extractor.config import ImagesConfig
from extractor.images.detect import Detector, scan_refs, select_columns

DET = Detector(ImagesConfig().extensions)


def test_bare_filename():
    assert DET.extract("20220324111256_83rxz.jpg") == ["20220324111256_83rxz.jpg"]


def test_html_attributes_and_srcset():
    html = "<img src=\"/uploads/a.jpg\"> <img src='b.png'> <img srcset=\"c.webp 1x, d.webp 2x\">"
    assert DET.extract(html) == ["/uploads/a.jpg", "b.png", "c.webp", "d.webp"]


def test_json_values():
    assert DET.extract('{"gallery": ["x/1.jpg", "x/2.png"], "n": 3}') == ["x/1.jpg", "x/2.png"]


def test_php_serialized():
    assert DET.extract('a:2:{i:0;s:8:"/a/1.jpg";i:1;s:5:"2.png";}') == ["/a/1.jpg", "2.png"]


def test_absolute_url_inside_text():
    assert DET.extract("see https://x.com/a/b.jpg?v=2 now.") == ["https://x.com/a/b.jpg?v=2"]


def test_plain_text_and_data_uri_have_no_refs():
    assert DET.extract("Rumah siap huni di Tebet Barat, dekat taman") == []
    assert DET.extract("data:image/png;base64,AAAA") == []
    assert DET.extract(None) == []


def test_column_selection_and_scan(dumped):
    dest, _ = dumped
    cfg = ImagesConfig()
    reports = select_columns(dest, cfg, DET)
    selected = {f"{r.table}.{r.column}" for r in reports if r.selected}
    assert selected == {"rumah.foto", "rumah_foto.foto", "rumah_foto.extra"}  # judul/deskripsi/harga excluded

    scan_refs(dest, DET, reports)
    with dest.connect() as c:
        n = c.exec_driver_sql("SELECT COUNT(*) FROM _image_refs").scalar()
    assert n == 7  # 3 + 2 + 2 (gallery)


def test_exclude_and_include_overrides(dumped):
    dest, _ = dumped
    cfg = ImagesConfig(exclude_columns=["rumah_foto.*"], include_columns=["rumah.deskripsi"])
    reports = {f"{r.table}.{r.column}": r for r in select_columns(dest, cfg, DET)}
    assert "rumah_foto.foto" not in reports
    assert reports["rumah.deskripsi"].forced and reports["rumah.deskripsi"].selected
