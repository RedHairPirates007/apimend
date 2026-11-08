import pizza_client


def test_menu_lists_pizzas():
    menu = pizza_client.get_menu()
    assert isinstance(menu, list)
    assert all("name" in p and "price" in p for p in menu)


def test_can_place_an_order():
    order = pizza_client.place_order("margherita", "large")
    assert "id" in order
    assert "status" in order