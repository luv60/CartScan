from blinkit_service import extract_blinkit_products_from_first_response


def test_extractor_keeps_starting_product_card_variant_schema():
    body = {
        "response": {
            "snippets": [
                {
                    "widget_type": "promoted_product_card_v3",
                    "data": {
                        "product": {
                            "id": "start-card-1",
                            "title": "Blinkit Starting Product",
                            "selling_price": {"text": "₹3"},
                            "mrp": {"text": "₹5"},
                            "pack_size": "100 g",
                        }
                    },
                }
            ]
        }
    }

    products = extract_blinkit_products_from_first_response(body, "product")

    assert len(products) == 1
    assert products[0]["product_id"] == "start-card-1"
    assert products[0]["name"] == "Blinkit Starting Product"
    assert products[0]["price"] == 3.0
    assert products[0]["packsize"] == "100 g"
