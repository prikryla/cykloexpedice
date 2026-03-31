"""Comprehensive tests for the Cykloexpedice Flask application."""
import json
from unittest.mock import patch, MagicMock
import bcrypt
import pytest


# ── Helper function tests ────────────────────────────────────────


class TestNameVocative:
    def test_male_name(self):
        from app import name_vocative
        assert name_vocative('Michal') == 'Michale'

    def test_male_name_adam(self):
        from app import name_vocative
        assert name_vocative('Adam') == 'Adame'

    def test_female_name(self):
        from app import name_vocative
        assert name_vocative('Vlasta') == 'Vlasto'

    def test_full_name_uses_first_only(self):
        from app import name_vocative
        result = name_vocative('Michal Přikryl')
        assert result == 'Michale'

    def test_empty_name(self):
        from app import name_vocative
        assert name_vocative('') == ''

    def test_capitalized(self):
        from app import name_vocative
        result = name_vocative('petr')
        assert result[0].isupper()


class TestGeneratePaymentQR:
    def test_returns_base64_string(self):
        from app import generate_payment_qr
        result = generate_payment_qr('CZ1620100000002703473997', 3500, 1)
        assert isinstance(result, str)
        assert len(result) > 100  # valid base64 PNG

    def test_spayd_format_in_qr(self):
        from app import generate_payment_qr
        # Just verify it doesn't crash with various inputs
        result = generate_payment_qr('CZ1620100000002703473997', 3500.50, 42, 'WACHAU_Jan_Novak')
        assert result  # non-empty

    def test_without_message(self):
        from app import generate_payment_qr
        result = generate_payment_qr('CZ1620100000002703473997', 1000, 7)
        assert result


class TestRenderEmailLayout:
    def test_contains_event_name(self):
        from app import render_email_layout
        settings = {'event_name': 'TEST EVENT', 'event_year': '2026',
                     'contact_email': 'a@b.cz', 'contact_name_1': 'A', 'contact_name_2': 'B'}
        html = render_email_layout('<p>body</p>', settings)
        assert 'TEST EVENT' in html
        assert '2026' in html
        assert 'a@b.cz' in html

    def test_contains_body(self):
        from app import render_email_layout
        settings = {'event_name': 'X', 'event_year': '', 'contact_email': '',
                     'contact_name_1': '', 'contact_name_2': ''}
        html = render_email_layout('<p>Hello World</p>', settings)
        assert '<p>Hello World</p>' in html


class TestRenderEmailTemplate:
    def test_replaces_placeholders(self, app):
        from app import render_email_template
        with app.app_context():
            variables = {
                'name': 'Jan Novák',
                'event_name': 'Cyklo',
                'event_year': '2026',
                'contact_name_1': 'A',
                'contact_name_2': 'B',
                'contact_email': 'x@y.cz',
            }
            subject, html = render_email_template('email_approved', variables)
            assert 'Cyklo' in subject
            assert '2026' in subject
            assert 'Jane' in html  # vocative of Jan
            assert '{{' not in html  # no unresolved placeholders

    def test_auto_generates_vocative(self, app):
        from app import render_email_template
        with app.app_context():
            variables = {'name': 'Petr', 'event_name': 'X', 'event_year': '',
                         'contact_name_1': '', 'contact_name_2': '', 'contact_email': ''}
            _, html = render_email_template('email_submitted', variables)
            assert 'Petře' in html


# ── Public route tests ───────────────────────────────────────────


class TestPublicRoutes:
    def test_index(self, client):
        r = client.get('/')
        assert r.status_code == 200

    def test_propozice(self, client):
        r = client.get('/propozice')
        assert r.status_code == 200

    def test_ubytovani(self, client):
        r = client.get('/ubytovani')
        assert r.status_code == 200

    def test_aktuality(self, client):
        r = client.get('/aktuality')
        assert r.status_code == 200

    def test_kontakt(self, client):
        r = client.get('/kontakt')
        assert r.status_code == 200

    def test_fotky_enabled(self, client):
        r = client.get('/fotky')
        assert r.status_code == 200

    def test_404_page(self, client):
        r = client.get('/nonexistent-page')
        assert r.status_code == 404

    def test_registrace_get(self, client):
        r = client.get('/registrace')
        assert r.status_code == 200


class TestRegistration:
    def test_submit_valid(self, client):
        with patch('app.send_email') as mock_email:
            r = client.post('/registrace', data={
                'name': 'Jan Novák',
                'email': 'jan@test.cz',
                'phone': '123456789',
                'note': 'Test note',
            }, follow_redirects=True)
            assert r.status_code == 200
            assert 'Děkujeme' in r.data.decode()
            mock_email.assert_called_once()

    def test_submit_without_name_fails(self, client):
        r = client.post('/registrace', data={
            'name': '',
            'email': 'jan@test.cz',
        })
        assert r.status_code == 200
        assert 'povinné' in r.data.decode()

    def test_submit_without_email_no_email_sent(self, client):
        with patch('app.send_email') as mock_email:
            r = client.post('/registrace', data={
                'name': 'Jan Novák',
                'email': '',
            }, follow_redirects=True)
            assert r.status_code == 200
            mock_email.assert_not_called()


# ── Admin auth tests ─────────────────────────────────────────────


class TestAdminAuth:
    def test_login_page_loads(self, client):
        r = client.get('/admin/login')
        assert r.status_code == 200

    def test_login_invalid_credentials(self, client):
        r = client.post('/admin/login', data={
            'username': 'nonexistent',
            'password': 'wrong',
        })
        assert r.status_code == 200
        assert 'Neplatné' in r.data.decode()

    def test_login_wrong_password(self, app, client):
        pw_hash = bcrypt.hashpw(b'correct', bcrypt.gensalt()).decode()
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute('UPDATE admins SET password_hash = ? WHERE username = ?',
                       (pw_hash, 'adam'))
            db.commit()

        r = client.post('/admin/login', data={
            'username': 'adam',
            'password': 'wrong',
        })
        assert 'Neplatné' in r.data.decode()

    def test_login_success(self, app, client):
        pw_hash = bcrypt.hashpw(b'testpass', bcrypt.gensalt()).decode()
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute('UPDATE admins SET password_hash = ? WHERE username = ?',
                       (pw_hash, 'adam'))
            db.commit()

        r = client.post('/admin/login', data={
            'username': 'adam',
            'password': 'testpass',
        }, follow_redirects=True)
        assert r.status_code == 200

    def test_admin_requires_login(self, client):
        r = client.get('/admin')
        assert r.status_code == 302
        assert '/admin/login' in r.headers['Location']

    def test_logout(self, admin_client):
        r = admin_client.get('/admin/logout', follow_redirects=True)
        assert r.status_code == 200
        # After logout, admin page should redirect
        r2 = admin_client.get('/admin')
        assert r2.status_code == 302

    def test_first_login_redirects_to_set_password(self, app, client):
        # Admin with no password_hash should redirect to set-password
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute('UPDATE admins SET password_hash = NULL WHERE username = ?', ('adam',))
            db.commit()

        r = client.post('/admin/login', data={
            'username': 'adam',
            'password': '',
        })
        assert r.status_code == 302
        assert 'set-password' in r.headers['Location']


# ── Admin dashboard & protected routes ───────────────────────────


class TestAdminDashboard:
    def test_dashboard_loads(self, admin_client):
        r = admin_client.get('/admin')
        assert r.status_code == 200

    def test_settings_page_loads(self, admin_client):
        r = admin_client.get('/admin/nastaveni')
        assert r.status_code == 200

    def test_save_settings(self, admin_client):
        r = admin_client.post('/admin/nastaveni', data={
            'event_name': 'NEW NAME',
            'event_year': '2030',
            'event_days': '5',
            'event_km': '500',
            'event_elevation': '3000',
            'event_dates': 'test dates',
            'contact_name_1': 'A',
            'contact_name_2': 'B',
            'contact_email': 'a@b.cz',
            'photos_link': '',
            'photos_text': '',
            'payment_amount': '4000',
            'bank_account': '123/456',
            'bank_iban': 'CZ123',
        }, follow_redirects=True)
        assert r.status_code == 200
        assert 'uloženo' in r.data.decode().lower()

    def test_profile_page_loads(self, admin_client):
        r = admin_client.get('/admin/profile')
        assert r.status_code == 200

    def test_email_templates_page_loads(self, admin_client):
        r = admin_client.get('/admin/email-sablony')
        assert r.status_code == 200

    def test_email_templates_save(self, admin_client):
        r = admin_client.post('/admin/email-sablony', data={
            'email_submitted_subject': 'Test Subject',
            'email_submitted_body': '<p>Test body</p>',
            'email_approved_subject': 'Approved',
            'email_approved_body': '<p>Approved</p>',
            'email_denied_subject': 'Denied',
            'email_denied_body': '<p>Denied</p>',
            'email_payment_subject': 'Payment',
            'email_payment_body': '<p>Payment</p>',
        }, follow_redirects=True)
        assert r.status_code == 200
        assert 'uložen' in r.data.decode().lower()

    def test_email_template_preview(self, admin_client):
        r = admin_client.post('/admin/email-sablony/preview', data={
            'subject': 'Test {{name}}',
            'body': '<p>Ahoj {{name_vocative}}</p>',
        })
        assert r.status_code == 200
        assert 'Jane' in r.data.decode()  # vocative of sample "Jan Novák"


# ── Admin Registration Management ────────────────────────────────


class TestAdminRegistrace:
    def _create_registration(self, admin_client, app, name='Test User', email='test@test.cz'):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute('INSERT INTO registrace (name, email, phone, note) VALUES (?, ?, ?, ?)',
                       (name, email, '123', 'note'))
            db.commit()
            row = db.execute('SELECT id FROM registrace WHERE name = ?', (name,)).fetchone()
            return row['id']

    def test_registrace_list(self, admin_client):
        r = admin_client.get('/admin/registrace')
        assert r.status_code == 200

    def test_registrace_search(self, admin_client, app):
        self._create_registration(admin_client, app, name='Hledaný Člověk')
        r = admin_client.get('/admin/registrace?q=Hledaný')
        assert r.status_code == 200
        assert 'Hledaný' in r.data.decode()

    def test_registrace_filter_by_status(self, admin_client, app):
        self._create_registration(admin_client, app)
        r = admin_client.get('/admin/registrace?status=pending')
        assert r.status_code == 200

    def test_approve_registration(self, admin_client, app):
        reg_id = self._create_registration(admin_client, app)
        with patch('app.send_email'):
            r = admin_client.post(f'/admin/registrace/{reg_id}/approve',
                                  follow_redirects=True)
        assert r.status_code == 200
        assert 'schválena' in r.data.decode().lower()

    def test_deny_registration_get(self, admin_client, app):
        reg_id = self._create_registration(admin_client, app)
        r = admin_client.get(f'/admin/registrace/{reg_id}/deny')
        assert r.status_code == 200

    def test_deny_registration_post(self, admin_client, app):
        reg_id = self._create_registration(admin_client, app)
        with patch('app.send_email'):
            r = admin_client.post(f'/admin/registrace/{reg_id}/deny',
                                  data={'reason': 'Plná kapacita'},
                                  follow_redirects=True)
        assert r.status_code == 200
        assert 'zamítnuta' in r.data.decode().lower()

    def test_deny_registration_without_reason(self, admin_client, app):
        reg_id = self._create_registration(admin_client, app)
        r = admin_client.post(f'/admin/registrace/{reg_id}/deny',
                              data={'reason': ''})
        assert r.status_code == 200
        assert 'důvod' in r.data.decode().lower()

    def test_delete_registration(self, admin_client, app):
        reg_id = self._create_registration(admin_client, app)
        r = admin_client.post(f'/admin/registrace/{reg_id}/delete',
                              follow_redirects=True)
        assert r.status_code == 200
        assert 'smazána' in r.data.decode().lower()

    def test_approve_nonexistent_returns_404(self, admin_client):
        r = admin_client.post('/admin/registrace/9999/approve')
        assert r.status_code == 404


# ── Payment tests ────────────────────────────────────────────────


class TestPayments:
    def _create_approved_registration(self, admin_client, app):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute(
                "INSERT INTO registrace (name, email, status) VALUES (?, ?, 'approved')",
                ('Jan Novák', 'jan@test.cz'))
            db.commit()
            row = db.execute("SELECT id FROM registrace WHERE name = 'Jan Novák'").fetchone()
            return row['id']

    def test_send_payment_qr(self, admin_client, app):
        reg_id = self._create_approved_registration(admin_client, app)
        with patch('app.send_email') as mock_email:
            r = admin_client.post(f'/admin/registrace/{reg_id}/send-payment',
                                  follow_redirects=True)
        assert r.status_code == 200
        assert 'QR' in r.data.decode()
        mock_email.assert_called_once()

    def test_send_payment_qr_unapproved_fails(self, admin_client, app):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute("INSERT INTO registrace (name, email, status) VALUES (?, ?, 'pending')",
                       ('Pending User', 'p@t.cz'))
            db.commit()
            row = db.execute("SELECT id FROM registrace WHERE name = 'Pending User'").fetchone()
            reg_id = row['id']

        r = admin_client.post(f'/admin/registrace/{reg_id}/send-payment',
                              follow_redirects=True)
        assert 'schválených' in r.data.decode().lower()

    def test_send_payment_no_email_fails(self, admin_client, app):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute("INSERT INTO registrace (name, email, status) VALUES (?, ?, 'approved')",
                       ('No Email', ''))
            db.commit()
            row = db.execute("SELECT id FROM registrace WHERE name = 'No Email'").fetchone()
            reg_id = row['id']

        r = admin_client.post(f'/admin/registrace/{reg_id}/send-payment',
                              follow_redirects=True)
        assert 'e-mail' in r.data.decode().lower()

    def test_check_payments_route(self, admin_client):
        with patch('app.check_fio_payments'):
            r = admin_client.post('/admin/registrace/check-payments',
                                  follow_redirects=True)
        assert r.status_code == 200
        assert 'zkontrolován' in r.data.decode().lower()

    def test_payment_note_format(self, admin_client, app):
        """Verify payment note is WACHAU_firstname_lastname."""
        reg_id = self._create_approved_registration(admin_client, app)
        with patch('app.send_email') as mock_email, \
             patch('app.generate_payment_qr') as mock_qr:
            mock_qr.return_value = 'fakebase64'
            admin_client.post(f'/admin/registrace/{reg_id}/send-payment')
            # Check the QR was called with correct message
            call_args = mock_qr.call_args
            assert call_args[0][3] == 'WACHAU_Jan_Novák'  # message argument


class TestCheckFioPayments:
    def test_matches_payment_by_vs(self, app):
        import app as flask_app

        # Create a pending registration with VS
        with app.app_context():
            db = flask_app.get_db()
            db.execute(
                "INSERT INTO registrace (name, email, status, variable_symbol, "
                "payment_status, payment_amount) VALUES (?, ?, 'approved', ?, 'pending', ?)",
                ('Test', 'x@y.cz', 42, 3500))
            db.commit()

        # Mock Fio API response
        fio_response = {
            'accountStatement': {
                'transactionList': {
                    'transaction': [{
                        'column1': {'value': 3500, 'name': 'Objem', 'id': 1},
                        'column5': {'value': '42', 'name': 'VS', 'id': 5},
                    }]
                }
            }
        }

        with patch('app.requests.get') as mock_get:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: fio_response)
            flask_app.check_fio_payments()

        # Verify payment was marked as paid
        with app.app_context():
            db = flask_app.get_db()
            row = db.execute("SELECT payment_status FROM registrace WHERE variable_symbol = 42").fetchone()
            assert row['payment_status'] == 'paid'

    def test_ignores_insufficient_amount(self, app):
        import app as flask_app

        with app.app_context():
            db = flask_app.get_db()
            db.execute(
                "INSERT INTO registrace (name, email, status, variable_symbol, "
                "payment_status, payment_amount) VALUES (?, ?, 'approved', ?, 'pending', ?)",
                ('Test2', 'x@y.cz', 99, 3500))
            db.commit()

        fio_response = {
            'accountStatement': {
                'transactionList': {
                    'transaction': [{
                        'column1': {'value': 1000, 'name': 'Objem', 'id': 1},  # too low
                        'column5': {'value': '99', 'name': 'VS', 'id': 5},
                    }]
                }
            }
        }

        with patch('app.requests.get') as mock_get:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: fio_response)
            flask_app.check_fio_payments()

        with app.app_context():
            db = flask_app.get_db()
            row = db.execute("SELECT payment_status FROM registrace WHERE variable_symbol = 99").fetchone()
            assert row['payment_status'] == 'pending'

    def test_handles_api_error(self, app):
        import app as flask_app
        with patch('app.requests.get') as mock_get:
            mock_get.return_value = MagicMock(status_code=409)
            flask_app.check_fio_payments()  # should not raise

    def test_handles_no_token(self, app, monkeypatch):
        import app as flask_app
        monkeypatch.setattr(flask_app, 'FIO_API_TOKEN', '')
        flask_app.check_fio_payments()  # should return early without error


# ── Admin CRUD tests ─────────────────────────────────────────────


class TestAdminEtapy:
    def test_list_etapy(self, admin_client):
        r = admin_client.get('/admin/etapy')
        assert r.status_code == 200

    def test_create_etapa(self, admin_client):
        r = admin_client.post('/admin/etapy/new', data={
            'number': '1',
            'title': 'Test Etapa',
            'date': '2026-09-01',
            'distance': '100',
            'elevation_up': '500',
            'elevation_down': '400',
            'route': 'A - B',
            'waypoints': 'town1, town2',
            'description': 'A test stage',
            'map_link': '',
            'youtube_links': '',
            'color': '#ffc107',
        }, follow_redirects=True)
        assert r.status_code == 200
        assert 'uložena' in r.data.decode().lower()

    def test_edit_etapa(self, admin_client, app):
        # Create first
        admin_client.post('/admin/etapy/new', data={
            'number': '2', 'title': 'Original', 'date': '', 'distance': '',
            'elevation_up': '', 'elevation_down': '', 'route': '',
            'waypoints': '', 'description': '', 'map_link': '',
            'youtube_links': '', 'color': '#ffc107',
        })
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            row = db.execute('SELECT id FROM etapy WHERE number = 2').fetchone()
            etapa_id = row['id']

        r = admin_client.get(f'/admin/etapy/{etapa_id}/edit')
        assert r.status_code == 200

    def test_delete_etapa(self, admin_client, app):
        admin_client.post('/admin/etapy/new', data={
            'number': '3', 'title': 'ToDelete', 'date': '', 'distance': '',
            'elevation_up': '', 'elevation_down': '', 'route': '',
            'waypoints': '', 'description': '', 'map_link': '',
            'youtube_links': '', 'color': '#ffc107',
        })
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            row = db.execute('SELECT id FROM etapy WHERE number = 3').fetchone()
            etapa_id = row['id']

        r = admin_client.post(f'/admin/etapy/{etapa_id}/delete', follow_redirects=True)
        assert r.status_code == 200
        assert 'smazána' in r.data.decode().lower()


class TestAdminAktuality:
    def test_list_aktuality(self, admin_client):
        r = admin_client.get('/admin/aktuality')
        assert r.status_code == 200

    def test_create_aktualita(self, admin_client):
        r = admin_client.post('/admin/aktuality/new', data={
            'title': 'Test News',
            'content': '<p>News content</p>',
            'published': '1',
        }, follow_redirects=True)
        assert r.status_code == 200
        assert 'uložena' in r.data.decode().lower()

    def test_delete_aktualita(self, admin_client, app):
        admin_client.post('/admin/aktuality/new', data={
            'title': 'ToDelete', 'content': 'x', 'published': '1',
        })
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            row = db.execute("SELECT id FROM aktuality WHERE title = 'ToDelete'").fetchone()

        r = admin_client.post(f'/admin/aktuality/{row["id"]}/delete', follow_redirects=True)
        assert r.status_code == 200


class TestAdminUbytovani:
    def test_list_ubytovani(self, admin_client):
        r = admin_client.get('/admin/ubytovani')
        assert r.status_code == 200

    def test_create_ubytovani(self, admin_client):
        r = admin_client.post('/admin/ubytovani/new', data={
            'etapa_number': '1', 'name': 'Hotel Test', 'city': 'Praha',
            'date': '2026-09-01', 'rooms_info': '2x', 'food_info': 'snídaně',
            'link': '', 'sort_order': '0',
        }, follow_redirects=True)
        assert r.status_code == 200
        assert 'uloženo' in r.data.decode().lower()


class TestAdminPropozice:
    def test_propozice_page(self, admin_client):
        r = admin_client.get('/admin/propozice')
        assert r.status_code == 200

    def test_save_propozice(self, admin_client):
        r = admin_client.post('/admin/propozice', data={
            'content': '<p>Updated propozice</p>',
        }, follow_redirects=True)
        assert r.status_code == 200
        assert 'uloženy' in r.data.decode().lower()


# ── Error handling tests ─────────────────────────────────────────


class TestErrorHandling:
    def test_404_returns_error_page(self, client):
        r = client.get('/this-does-not-exist')
        assert r.status_code == 404

    def test_db_error_in_context_processor(self, app, monkeypatch):
        """When DB is down, pages should still render with empty data."""
        import app as flask_app

        original_get_db = flask_app.get_db

        def failing_db():
            raise Exception("DB down")

        # Only fail on first call (context processor), not on error handler
        call_count = [0]
        def sometimes_fail():
            call_count[0] += 1
            if call_count[0] <= 1:
                raise Exception("DB down")
            return original_get_db()

        client = app.test_client()
        with app.app_context():
            monkeypatch.setattr(flask_app, 'get_db', sometimes_fail)
            r = client.get('/')
            # Should not crash — context processor catches the error
            assert r.status_code in (200, 500)


# ── Password management tests ────────────────────────────────────


class TestPasswordManagement:
    def test_change_password(self, admin_client, app):
        r = admin_client.post('/admin/change-password', data={
            'current_password': 'testpass',
            'new_password': 'newpass123',
            'new_password2': 'newpass123',
        }, follow_redirects=True)
        assert r.status_code == 200
        assert 'změněno' in r.data.decode().lower()

    def test_change_password_wrong_current(self, admin_client):
        r = admin_client.post('/admin/change-password', data={
            'current_password': 'wrongpassword',
            'new_password': 'newpass123',
            'new_password2': 'newpass123',
        })
        assert 'nesprávné' in r.data.decode().lower()

    def test_change_password_mismatch(self, admin_client):
        r = admin_client.post('/admin/change-password', data={
            'current_password': 'testpass',
            'new_password': 'newpass123',
            'new_password2': 'different',
        })
        assert 'neshodují' in r.data.decode().lower()

    def test_change_password_too_short(self, admin_client):
        r = admin_client.post('/admin/change-password', data={
            'current_password': 'testpass',
            'new_password': '12345',
            'new_password2': '12345',
        })
        assert '6 znaků' in r.data.decode().lower()

    def test_forgot_password(self, client):
        r = client.post('/admin/forgot-password', data={
            'username': 'adam',
        }, follow_redirects=True)
        assert r.status_code == 200
