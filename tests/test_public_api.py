def test_public_api_exports_scraper_types():
    from korean_law_data_tools import CollectSettings, LawScraper

    assert LawScraper.__name__ == "LawScraper"
    assert CollectSettings.__name__ == "CollectSettings"
