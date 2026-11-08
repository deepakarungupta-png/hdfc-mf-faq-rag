import pytest

from app.sources import is_allowed_host


@pytest.mark.parametrize(
    "url",
    [
        "https://www.hdfcfund.com/explore/mutual-funds/hdfc-mid-cap-fund/direct",
        "https://files.hdfcfund.com/s3fs-public/x.pdf",
        "https://hdfcfund.com/",
        "https://investor.sebi.gov.in/riskometer.html",
        "https://www.sebi.gov.in/sebi_data/faqfiles/x.pdf",
        "https://www.amfiindia.com/uploads/x.pdf",
        "https://WWW.HDFCFUND.COM/",
        "https://www.hdfcfund.com:443/",
    ],
)
def test_allowed(url):
    assert is_allowed_host(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://evilhdfcfund.com/",
        "https://hdfcfund.com.example.net/",
        "http://www.hdfcfund.com/",
        "https://example.com/?u=hdfcfund.com",
        "https://hdfcfund.com@example.com/",
        "https://notsebi.gov.in/",
        "ftp://www.amfiindia.com/",
        "",
        "not a url",
    ],
)
def test_rejected(url):
    assert not is_allowed_host(url)
