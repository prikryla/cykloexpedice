"""Seed the database with data from the existing cykloexpedice.cz website."""
import json
import os
import sqlite3

DATABASE_URL = os.environ.get('DATABASE_URL', '')
DATABASE = os.path.join(os.path.dirname(__file__), 'cykloexpedice.db')


def seed(db=None):
    """Seed database. Accepts an existing db connection or creates its own."""
    close_after = False
    if db is None:
        db = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
        close_after = True

    # ── Etapy ──────────────────────────────────────────────────
    placeholder_desc = '<p>Podrobný popis trasy se připravuje a bude doplněn v nejbližší době. Sledujte aktuality.</p>'

    etapy = [
        {
            'number': 1,
            'title': 'První etapa',
            'date': 'Bude upřesněno',
            'distance': '–',
            'elevation_up': '–',
            'elevation_down': '–',
            'route': 'Bude upřesněno',
            'waypoints': 'Připravujeme',
            'description': placeholder_desc,
            'map_link': '',
            'youtube_links': json.dumps([]),
            'color': '#ffc107',
        },
        {
            'number': 2,
            'title': 'Druhá etapa',
            'date': 'Bude upřesněno',
            'distance': '–',
            'elevation_up': '–',
            'elevation_down': '–',
            'route': 'Bude upřesněno',
            'waypoints': 'Připravujeme',
            'description': placeholder_desc,
            'map_link': '',
            'youtube_links': json.dumps([]),
            'color': '#e5e7eb',
        },
        {
            'number': 3,
            'title': 'Třetí etapa',
            'date': 'Bude upřesněno',
            'distance': '–',
            'elevation_up': '–',
            'elevation_down': '–',
            'route': 'Bude upřesněno',
            'waypoints': 'Připravujeme',
            'description': placeholder_desc,
            'map_link': '',
            'youtube_links': json.dumps([]),
            'color': '#06b6d4',
        },
    ]

    for e in etapy:
        existing = db.execute('SELECT id FROM etapy WHERE number = ?', (e['number'],)).fetchone()
        if existing:
            db.execute(
                '''UPDATE etapy SET title=?, date=?, distance=?, elevation_up=?, elevation_down=?,
                   route=?, waypoints=?, description=?, map_link=?, youtube_links=?, color=?
                   WHERE number=?''',
                (e['title'], e['date'], e['distance'], e['elevation_up'],
                 e['elevation_down'], e['route'], e['waypoints'], e['description'],
                 e['map_link'], e['youtube_links'], e['color'], e['number'])
            )
        else:
            db.execute(
                '''INSERT INTO etapy (number, title, date, distance, elevation_up, elevation_down,
                   route, waypoints, description, map_link, youtube_links, color)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (e['number'], e['title'], e['date'], e['distance'], e['elevation_up'],
                 e['elevation_down'], e['route'], e['waypoints'], e['description'],
                 e['map_link'], e['youtube_links'], e['color'])
            )

    # ── Propozice ──────────────────────────────────────────────
    existing = db.execute('SELECT id FROM propozice LIMIT 1').fetchone()
    if not existing:
        propozice_html = '''<h2>Staročeská cykloexpedice 2025</h2>

<p><strong><i class="bi bi-calendar3"></i> Termín:</strong> 11. – 13. září 2025</p>
<p><strong><i class="bi bi-signpost-2"></i> Odjezd:</strong> parkoviště u Billy v Blansku, Svitavská 2307/16</p>
<p><strong><i class="bi bi-clock"></i> Sraz:</strong> v 4:30 hod. ve čtvrtek 11.09.2025</p>

<p>Dopravu na místo startu cykloexpedice autobusem s cyklovlekem zajišťuje pan Veselý ze společnosti <a href="https://vesbus.webnode.cz/" target="_blank">Vesbus</a>.</p>

<p>Cesta na start cykloexpedice bude trvat zhruba 4 hodiny.</p>

<p>Trasa expedice je rozložena do 3 etap, rovinatých 97 km od hory ŘÍP do Poděbrad, pak 93 km z Poděbrad do Ždírce nad Doubravou a závěrečných zvlněných 97 km ze Ždírce do Blanska.</p>

<h3>Přihlášky</h3>
<p>Závazné přihlášky posílejte emailem do 15.6.2025 na <a href="mailto:info@cykloexpedice.cz">info@cykloexpedice.cz</a>.</p>
<p>Jakmile budete rozhodnutí, pak čím dříve nám dáte vědět, tím lépe, velmi nám tak ulehčíte organizaci celé akce. Předem děkujeme.</p>

<h3>Cena</h3>
<p>Do 01.07.2025 přihlášeným účastníkům zašleme emailem přesnou cenu akce, podle počtu expedičníků a tedy rozložení nákladů.</p>
<p>Obě ubytování pro letošek mírně navýšily ceny, celková cena se tak bude pohybovat nejpravděpodobněji do 3 500 Kč na osobu (loni 3 380 Kč na osobu).</p>
<p>Celková cena bude pokrývat cestu na start a dvoje ubytování se snídaní.</p>

<h3>Platba</h3>
<p>Nejpozději do 20.07.2025 pak děkujeme za úhradu (detaily přiložíme v emailu dne 01.07.2025), kterou po závazném přihlášení na cykloexpedici považujte, prosím, za nevratnou. Děkujeme za pochopení.</p>'''
        db.execute('INSERT INTO propozice (content) VALUES (?)', (propozice_html,))

    # ── Ubytování ──────────────────────────────────────────────
    ubytovani = [
        {
            'etapa_number': 1,
            'name': 'Penzion Na hrázi',
            'city': 'Poděbrady',
            'date': 'čtvrtek, 11.09.2025',
            'rooms_info': '2lůžkové – 4lůžkové pokoje, s vlastním sociálním zařízením. Kola uložíme na dvoře penzionu, střeženo kamerami.',
            'food_info': 'V restauraci v penzionu, večeři budeme mít formou společného menu, posezení po večeři zajištěno. Snídaně formou bufetu tamtéž.',
            'link': 'https://podebrady-ubytovani.cz/na-hrazi/',
            'sort_order': 1,
        },
        {
            'etapa_number': 2,
            'name': 'Hotel Filippi',
            'city': 'Ždírec nad Doubravou',
            'date': 'pátek, 12.09.2025',
            'rooms_info': '2lůžkové – 4lůžkové pokoje s vlastní koupelnou a WC. Kola uložíme v místnosti v zázemí hotelu.',
            'food_info': 'Hotel Filippi letos neprovozuje restauraci. Na večeři a večerní posezení je zamluvena restaurace <a href="https://bowling-bar-zdirec.cz/restaurace/" target="_blank">Bowling bar</a>, asi 10 minut chůze od hotelu Filippi. Rezervace je od 18:00 hod.',
            'link': 'http://www.hotel-filippi.cz/index.htm',
            'sort_order': 2,
        },
    ]

    for u in ubytovani:
        existing = db.execute('SELECT id FROM ubytovani WHERE name = ?', (u['name'],)).fetchone()
        if not existing:
            db.execute(
                '''INSERT INTO ubytovani (etapa_number, name, city, date, rooms_info, food_info, link, sort_order)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                (u['etapa_number'], u['name'], u['city'], u['date'],
                 u['rooms_info'], u['food_info'], u['link'], u['sort_order'])
            )

    # ── Aktuality ──────────────────────────────────────────────
    existing = db.execute('SELECT id FROM aktuality LIMIT 1').fetchone()
    if not existing:
        aktualita_html = '''<h4><strong>K ubytování v PENZIONU NA HRÁZI v Poděbradech:</strong></h4>
<p>V restauraci je zároveň recepce, kde dostanete kartičku na vstup do pokoje a do dvora, kde budeme ukládat kola. Ve dvoře je velké zastřešené stání pro několik aut, kde kola necháme. Celý penzion je ten den pro nás, takže auta našim kolům zavazet nebudou. Dvůr je pod kamerami.</p>
<p>Večeře bude vydávána průběžně, jak přijdete do restaurace a objednáte si.</p>
<p>V pátek 12.9. ráno pak budeme mít v restauraci od 7:30 hod. připravenou snídani, servírováno bufetem/švédskými stoly.</p>

<h4><strong>K ubytování v HOTELu FILIPPI ve Ždírci nad Doubravou:</strong></h4>
<p>Kola dáme dovnitř do velké místnosti, v zadní části hotelu.</p>
<p>Pan Filippi žádá majitele elektrokol, aby si vzali z kol baterky na pokoje a nabíjeli je tam.</p>
<p>Snídaně v sobotu 13.9. budou v hotelu připraveny od 7:30 hod., opět výběr z bufetu.</p>
<p>Zamluvili jsme Bowling bar (Žďárská 622, Ždírec n.D.), kde máme od 18:00 hod. zamluveny stoly.</p>

<p>Takže ve čtvrtek nad ránem v 4:30 hod. se těšíme na setkání na parkovišti u Billy Blansko.</p>
<p>Michal a Adam</p>'''
        db.execute(
            'INSERT INTO aktuality (title, content, published) VALUES (?, ?, 1)',
            ('Poslední info před odjezdem', aktualita_html)
        )

    db.commit()
    if close_after:
        db.close()
    print('Database seeded successfully!')


if __name__ == '__main__':
    seed()
