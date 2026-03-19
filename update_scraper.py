import json, re, time

# This script replaces the GoogleFlightsScraper class in skyscanner.py with an advanced version.

with open('skyscanner.py', 'r') as f:
    text = f.read()

start_idx = text.find('class GoogleFlightsScraper:')
end_idx = text.find('class Monitor:')

new_scraper = '''class GoogleFlightsScraper:
    def __init__(self, browser):
        self.browser = browser

    def _accept_cookies_if_present(self, page) -> None:
        labels = ["Aceitar tudo", "Aceito", "I agree", "Accept all"]
        for label in labels:
            try:
                page.get_by_role("button", name=label).click(timeout=2000)
                time.sleep(1)
                return
            except Exception:
                pass

    def _extract_summary_price(self, page) -> float | None:
        patterns = [
            r"Menores preços\s+a partir de\s+R\$\s*([\d\.]+(?:,\d{2})?)",
            r"Menores preços.*?R\$\s*([\d\.]+(?:,\d{2})?)",
        ]
        for sel in ["body", "main", "[role='main']"]:
            try:
                txt = page.locator(sel).first.inner_text(timeout=3000)
                if not txt: continue
                for pattern in patterns:
                    m = re.search(pattern, txt, flags=re.IGNORECASE | re.DOTALL)
                    if m:
                        try:
                            return float(m.group(1).replace(".", "").replace(",", "."))
                        except ValueError:
                            pass
            except Exception:
                pass
        return None

    def _click_lowest_prices_tab(self, page) -> bool:
        candidates = [
            lambda: page.get_by_text("Menores preços", exact=False),
            lambda: page.get_by_role("button", name=re.compile(r"Menores preços", re.I)),
            lambda: page.get_by_role("tab", name=re.compile(r"Menores preços", re.I)),
        ]
        for factory in candidates:
            try:
                loc = factory()
                if loc.count() > 0:
                    loc.first.click(timeout=4000)
                    time.sleep(2.5)
                    try:
                        page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass
                    time.sleep(2.0)
                    return True
            except Exception:
                pass
        return False

    def _extract_visible_flight_cards(self, page) -> list[dict]:
        cards = []
        selectors = ["[role='listitem']", "li", "div[jscontroller]", "div[role='button']"]
        
        for sel in selectors:
            try:
                loc = page.locator(sel)
                count = min(loc.count(), 120)
                for i in range(count):
                    card = loc.nth(i)
                    try:
                        txt = card.inner_text(timeout=1200).strip()
                    except Exception:
                        continue
                    
                    if not txt or "R$" not in txt: continue
                    low = txt.lower()
                    if any(x in low for x in ["menores preços", "histórico", "gráfico", "monitorar"]):
                        continue
                    
                    has_shape = any(x in low for x in ["parada", "escalas", "co2", "emissões"])
                    if not has_shape: continue
                    
                    nums = re.findall(r"R\$\s*([\d\.]+(?:,\d{2})?)", txt)
                    if not nums: continue
                    try:
                        price = float(nums[-1].replace('.', '').replace(',', '.'))
                        cards.append({"selector": sel, "index": i, "price": price, "loc": card})
                    except Exception:
                        pass
            except Exception:
                pass
            if cards: break
        return cards

    def _open_card_and_extract_vendor(self, page, card) -> tuple[str, float | None, list[dict]]:
        try:
            card.click(timeout=4000)
            time.sleep(2.5)
        except Exception:
            pass
            
        try:
            btns = card.locator("button")
            if btns.count() > 0:
                btns.last.click(timeout=3000)
                time.sleep(2.0)
        except Exception:
            pass
            
        action_labels = ["Selecionar voo", "Ver voos", "Selecionar", "Reservar", "Opções de reserva"]
        for label in action_labels:
            try:
                loc = page.get_by_role("button", name=label)
                if loc.count() > 0:
                    loc.first.click(timeout=3000)
                    time.sleep(2.5)
                    break
            except Exception:
                pass
            try:
                loc = page.get_by_role("link", name=label)
                if loc.count() > 0:
                    loc.first.click(timeout=3000)
                    time.sleep(2.5)
                    break
            except Exception:
                pass

        try:
            body = page.locator("body").inner_text(timeout=7000)
        except Exception:
            return "", None, []

        options = []
        patterns = [
            r"Reserve com a\s+([^\n\r]+?)\s+R\$\s*([\d\.]+(?:,\d{2})?)",
            r"Reservar com\s+([^\n\r]+?)\s+R\$\s*([\d\.]+(?:,\d{2})?)",
        ]
        for pattern in patterns:
            for vendor, raw_price in re.findall(pattern, body, flags=re.IGNORECASE):
                vendor = (vendor or "").strip()
                try:
                    price = float(raw_price.replace(".", "").replace(",", "."))
                except Exception:
                    continue
                if vendor:
                    options.append({"vendor": vendor, "price": price})
        
        cleaned = []
        seen = set()
        for item in options:
            key = (item["vendor"].lower(), item["price"])
            if key not in seen:
                seen.add(key)
                cleaned.append(item)
                
        if not cleaned:
            return "", None, []
            
        best = sorted(cleaned, key=lambda x: x["price"])[0]
        return best["vendor"], best["price"], cleaned

    def _open_best_flight_details_if_possible(self, page) -> None:
        for role, label in [("button", "Selecionar voo"), ("button", "Ver voos"), ("link", "Selecionar voo"), ("link", "Ver voos")]:
            try:
                locator = page.get_by_role(role, name=label)
                if locator.count() > 0:
                    locator.first.click(timeout=2500)
                    time.sleep(2)
                    return
            except Exception:
                pass

    def search(self, route: RouteQuery) -> FlightResult:
        page = self.browser.new_page(locale="pt-BR")
        page.set_default_timeout(int(CONFIG["timeout_ms"]))
        url = build_google_flights_url(route)
        notes = []
        try:
            page.goto(url, wait_until="domcontentloaded")
            self._accept_cookies_if_present(page)
            try:
                page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass
            time.sleep(CONFIG["settle_seconds"])

            summary_price = self._extract_summary_price(page)
            if summary_price is not None:
                notes.append(f"summary_price={format_brl(summary_price)}")
            else:
                notes.append("summary_price=N/D")

            clicked_lowest = self._click_lowest_prices_tab(page)
            notes.append(f"clicou_menores_precos={'sim' if clicked_lowest else 'nao'}")

            best_vendor = ""
            best_vendor_price = None
            booking_options = []
            final_price = None

            cards = self._extract_visible_flight_cards(page)
            if summary_price is not None and cards:
                # Find matching price
                for item in cards:
                    if abs(item["price"] - summary_price) < 0.01:
                        final_price = item["price"]
                        notes.append(f"card_preco_encontrado={format_brl(final_price)}")
                        best_vendor, best_vendor_price, booking_options = self._open_card_and_extract_vendor(page, item["loc"])
                        break
            
            if final_price is None and cards:
                cheapest = sorted(cards, key=lambda x: x["price"])[0]
                final_price = cheapest["price"]
                notes.append(f"fallback_list_min_price={format_brl(final_price)}")
                best_vendor, best_vendor_price, booking_options = self._open_card_and_extract_vendor(page, cheapest["loc"])

            if best_vendor:
                notes.append(f"melhor_vendedor={best_vendor} ({format_brl(best_vendor_price)})")

            if not best_vendor:
                self._open_best_flight_details_if_possible(page)
                v2, p2, options2 = self._open_card_and_extract_vendor(page, page.locator("body"))
                if v2:
                    best_vendor = v2
                    best_vendor_price = p2
                    booking_options = options2
                    notes.append(f"fallback_global_melhor_vendedor={best_vendor} ({format_brl(best_vendor_price)})")

            if best_vendor_price is not None and final_price is None:
                final_price = best_vendor_price

            if final_price is None:
                notes.append("Preço não identificado automaticamente.")

            return FlightResult(
                site="google_flights",
                origin=route.origin,
                destination=route.destination,
                outbound_date=route.outbound_date,
                inbound_date=route.inbound_date,
                trip_type=route.trip_type,
                price=final_price,
                currency="BRL",
                url=page.url,
                notes=" | ".join(notes),
                best_vendor=best_vendor,
                best_vendor_price=best_vendor_price,
                booking_options_json=json.dumps(booking_options, ensure_ascii=False) if booking_options else "",
            )
        except PlaywrightTimeoutError:
            return FlightResult(
                site="google_flights",
                origin=route.origin,
                destination=route.destination,
                outbound_date=route.outbound_date,
                inbound_date=route.inbound_date,
                trip_type=route.trip_type,
                price=None,
                currency="BRL",
                url=page.url if page else url,
                notes="timeout na página",
            )
        finally:
            page.close()

'''

new_text = text[:start_idx] + new_scraper + text[end_idx:]
with open('skyscanner.py', 'w') as f:
    f.write(new_text)

print("Updated skyscanner.py")
