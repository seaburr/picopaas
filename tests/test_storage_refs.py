from app import crypto, models
from app.reconciler import _storage_refs


def _app(endpoint):
    row = models.AppStorage(app_id=1, bucket="b", endpoint=endpoint, region="atl1", access_key="k",
                            secret_key_encrypted=crypto.encrypt("s"))
    return models.App(storage=row)


def test_storage_endpoint_gets_scheme():
    assert _storage_refs(_app("atl1.digitaloceanspaces.com"))["storage.endpoint"] == \
        "https://atl1.digitaloceanspaces.com"
    assert _storage_refs(_app("https://atl1.digitaloceanspaces.com"))["storage.endpoint"] == \
        "https://atl1.digitaloceanspaces.com"
