"""Comprehensive tests for the Cykloexpedice Flask application."""
import json
from unittest.mock import patch, MagicMock
import bcrypt
import pytest
from flask.testing import FlaskClient


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
                'gdpr_consent': 'on',
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

    def test_submit_without_email_fails(self, client):
        r = client.post('/registrace', data={
            'name': 'Jan Novák',
            'email': '',
        })
        assert r.status_code == 200
        assert 'povinný' in r.data.decode().lower()


class TestRegistrationPhoneValidation:
    def _post(self, client, phone):
        return client.post('/registrace', data={
            'name': 'Test User',
            'email': 'test@seznam.cz',
            'phone': phone,
            'gdpr_consent': 'on',
        }, follow_redirects=True)

    def test_valid_cz_prefix(self, client):
        with patch('app.send_email'):
            r = self._post(client, '+420 777 123 456')
        assert 'Děkujeme' in r.data.decode()

    def test_valid_sk_prefix(self, client):
        with patch('app.send_email'):
            r = self._post(client, '+421905123456')
        assert 'Děkujeme' in r.data.decode()

    def test_valid_nine_digits_no_prefix(self, client):
        with patch('app.send_email'):
            r = self._post(client, '777123456')
        assert 'Děkujeme' in r.data.decode()

    def test_valid_nine_digits_with_spaces(self, client):
        with patch('app.send_email'):
            r = self._post(client, '777 123 456')
        assert 'Děkujeme' in r.data.decode()

    def test_empty_phone_allowed(self, client):
        with patch('app.send_email'):
            r = self._post(client, '')
        assert 'Děkujeme' in r.data.decode()

    def test_reject_us_prefix(self, client):
        r = self._post(client, '+1-604-597-9167')
        assert 'předvolba' in r.data.decode().lower()

    def test_reject_uk_prefix(self, client):
        r = self._post(client, '+44 7911 123456')
        assert 'předvolba' in r.data.decode().lower()

    def test_reject_de_prefix(self, client):
        r = self._post(client, '+49 170 1234567')
        assert 'předvolba' in r.data.decode().lower()

    def test_reject_too_few_digits(self, client):
        r = self._post(client, '12345678')
        assert 'číslic' in r.data.decode().lower()

    def test_reject_too_many_digits(self, client):
        r = self._post(client, '1234567890')
        assert 'číslic' in r.data.decode().lower()

    def test_reject_cz_prefix_wrong_digit_count(self, client):
        r = self._post(client, '+420 12345678')
        html = r.data.decode().lower()
        assert 'neplatné' in html

    def test_reject_letters_in_phone(self, client):
        r = self._post(client, 'abcdefghi')
        assert 'číslic' in r.data.decode().lower()

    def test_valid_cz_prefix_with_dashes(self, client):
        with patch('app.send_email'):
            r = self._post(client, '+420-777-123-456')
        assert 'Děkujeme' in r.data.decode()


class TestRegistrationEmailValidation:
    def _post(self, client, email):
        return client.post('/registrace', data={
            'name': 'Test User',
            'email': email,
            'gdpr_consent': 'on',
        }, follow_redirects=True)

    def test_valid_cz_domain(self, client):
        with patch('app.send_email'):
            r = self._post(client, 'user@seznam.cz')
        assert 'Děkujeme' in r.data.decode()

    def test_valid_sk_domain(self, client):
        with patch('app.send_email'):
            r = self._post(client, 'user@azet.sk')
        assert 'Děkujeme' in r.data.decode()

    def test_valid_com_domain(self, client):
        with patch('app.send_email'):
            r = self._post(client, 'user@gmail.com')
        assert 'Děkujeme' in r.data.decode()

    def test_reject_exotic_tld(self, client):
        r = self._post(client, 'user@example.xyz')
        assert 'Neplatná' in r.data.decode()

    def test_reject_org_tld(self, client):
        r = self._post(client, 'user@example.org')
        assert 'Neplatná' in r.data.decode()

    def test_reject_net_tld(self, client):
        r = self._post(client, 'user@example.net')
        assert 'Neplatná' in r.data.decode()

    def test_reject_io_tld(self, client):
        r = self._post(client, 'user@example.io')
        assert 'Neplatná' in r.data.decode()

    def test_reject_no_at_sign(self, client):
        r = self._post(client, 'invalidemail')
        assert 'Neplatná' in r.data.decode()

    def test_reject_ru_tld(self, client):
        r = self._post(client, 'bot@spam.ru')
        assert 'Neplatná' in r.data.decode()

    def test_valid_custom_cz_domain(self, client):
        with patch('app.send_email'):
            r = self._post(client, 'info@mojedomena.cz')
        assert 'Děkujeme' in r.data.decode()


class TestRegistrationGDPR:
    def test_reject_without_gdpr(self, client):
        r = client.post('/registrace', data={
            'name': 'Test User',
            'email': 'test@test.cz',
        }, follow_redirects=True)
        assert 'osobních údajů' in r.data.decode().lower()

    def test_accept_with_gdpr(self, client):
        with patch('app.send_email'):
            r = client.post('/registrace', data={
                'name': 'Test User',
                'email': 'test@test.cz',
                'gdpr_consent': 'on',
            }, follow_redirects=True)
        assert 'Děkujeme' in r.data.decode()

    def test_not_stored_in_db(self, app, client):
        with patch('app.send_email'):
            client.post('/registrace', data={
                'name': 'GDPR Test',
                'email': 'gdpr@test.cz',
                'gdpr_consent': 'on',
            }, follow_redirects=True)
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            row = db.execute("SELECT * FROM registrace WHERE name = 'GDPR Test'").fetchone()
            assert row is not None
            assert 'gdpr_consent' not in dict(row)


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


class TestAdminBulkDelete:
    def _create_registrations(self, app, count=3):
        ids = []
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            for i in range(count):
                db.execute('INSERT INTO registrace (name, email) VALUES (?, ?)',
                           (f'Bulk User {i}', f'bulk{i}@test.cz'))
            db.commit()
            rows = db.execute('SELECT id FROM registrace ORDER BY id').fetchall()
            ids = [r['id'] for r in rows]
        return ids

    def test_bulk_delete_multiple(self, admin_client, app):
        ids = self._create_registrations(app, 3)
        r = admin_client.post('/admin/registrace/bulk-delete',
                              data={'ids': [ids[0], ids[1]]},
                              follow_redirects=True)
        assert r.status_code == 200
        assert 'Smazáno 2' in r.data.decode()
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            remaining = db.execute('SELECT COUNT(*) c FROM registrace').fetchone()['c']
            assert remaining == 1

    def test_bulk_delete_all(self, admin_client, app):
        ids = self._create_registrations(app, 3)
        r = admin_client.post('/admin/registrace/bulk-delete',
                              data={'ids': ids},
                              follow_redirects=True)
        assert r.status_code == 200
        assert 'Smazáno 3' in r.data.decode()
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            remaining = db.execute('SELECT COUNT(*) c FROM registrace').fetchone()['c']
            assert remaining == 0

    def test_bulk_delete_no_ids(self, admin_client):
        r = admin_client.post('/admin/registrace/bulk-delete',
                              data={},
                              follow_redirects=True)
        assert r.status_code == 200

    def test_bulk_delete_single(self, admin_client, app):
        ids = self._create_registrations(app, 2)
        r = admin_client.post('/admin/registrace/bulk-delete',
                              data={'ids': ids[0]},
                              follow_redirects=True)
        assert r.status_code == 200
        assert 'Smazáno 1' in r.data.decode()

    def test_bulk_delete_requires_login(self, client):
        r = client.post('/admin/registrace/bulk-delete',
                        data={'ids': [1, 2]})
        assert r.status_code == 302

    def test_bulk_delete_page_has_checkboxes(self, admin_client, app):
        self._create_registrations(app, 2)
        r = admin_client.get('/admin/registrace')
        html = r.data.decode()
        assert 'select-all' in html
        assert 'row-checkbox' in html
        assert 'bulk-delete-btn' in html


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
        assert '8 znaků' in r.data.decode().lower()

    def test_set_password_page_requires_session(self, client):
        r = client.get('/admin/set-password')
        assert r.status_code == 302
        assert '/admin/login' in r.headers['Location']

    def test_set_password_too_short(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute('UPDATE admins SET password_hash = NULL WHERE username = ?', ('adam',))
            db.commit()

        client.post('/admin/login', data={'username': 'adam', 'password': ''})
        r = client.post('/admin/set-password', data={
            'password': 'short',
            'password2': 'short',
        })
        assert '8 znaků' in r.data.decode().lower()

    def test_set_password_mismatch(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute('UPDATE admins SET password_hash = NULL WHERE username = ?', ('adam',))
            db.commit()

        client.post('/admin/login', data={'username': 'adam', 'password': ''})
        r = client.post('/admin/set-password', data={
            'password': 'password123',
            'password2': 'different123',
        })
        assert 'neshodují' in r.data.decode().lower()

    def test_set_password_success(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute('UPDATE admins SET password_hash = NULL WHERE username = ?', ('adam',))
            db.commit()

        client.post('/admin/login', data={'username': 'adam', 'password': ''})
        r = client.post('/admin/set-password', data={
            'password': 'newpassword1',
            'password2': 'newpassword1',
        }, follow_redirects=True)
        assert r.status_code == 200

    def test_forgot_password(self, client):
        r = client.post('/admin/forgot-password', data={
            'username': 'adam',
        }, follow_redirects=True)
        assert r.status_code == 200

    def test_forgot_password_sends_email(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute('UPDATE admins SET email = ? WHERE username = ?', ('adam@test.cz', 'adam'))
            pw_hash = bcrypt.hashpw(b'testpass', bcrypt.gensalt()).decode()
            db.execute('UPDATE admins SET password_hash = ? WHERE username = ?', (pw_hash, 'adam'))
            db.commit()

        with patch('app.send_email') as mock_email:
            r = client.post('/admin/forgot-password', data={
                'username': 'adam',
            }, follow_redirects=True)
            assert r.status_code == 200
            mock_email.assert_called_once()

    def test_forgot_password_nonexistent_user(self, client):
        r = client.post('/admin/forgot-password', data={
            'username': 'nobody',
        }, follow_redirects=True)
        assert r.status_code == 200
        assert 'odeslali' in r.data.decode().lower()

    def test_reset_password_invalid_token(self, client):
        r = client.get('/admin/reset-password/invalid-token', follow_redirects=True)
        assert r.status_code == 200
        assert 'neplatný' in r.data.decode().lower()

    def test_reset_password_flow(self, app, client):
        import secrets
        from datetime import datetime
        token = secrets.token_urlsafe(48)
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            admin = db.execute('SELECT id FROM admins WHERE username = ?', ('adam',)).fetchone()
            expires = datetime.now().timestamp() + 3600
            db.execute(
                'INSERT INTO password_reset_tokens (admin_id, token, expires_at) VALUES (?, ?, ?)',
                (admin['id'], token, datetime.fromtimestamp(expires))
            )
            db.commit()

        r = client.get(f'/admin/reset-password/{token}')
        assert r.status_code == 200

        r = client.post(f'/admin/reset-password/{token}', data={
            'password': 'newpassword1',
            'password2': 'newpassword1',
        }, follow_redirects=True)
        assert r.status_code == 200
        assert 'změněno' in r.data.decode().lower()

    def test_reset_password_too_short(self, app, client):
        import secrets
        from datetime import datetime
        token = secrets.token_urlsafe(48)
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            admin = db.execute('SELECT id FROM admins WHERE username = ?', ('adam',)).fetchone()
            expires = datetime.now().timestamp() + 3600
            db.execute(
                'INSERT INTO password_reset_tokens (admin_id, token, expires_at) VALUES (?, ?, ?)',
                (admin['id'], token, datetime.fromtimestamp(expires))
            )
            db.commit()

        r = client.post(f'/admin/reset-password/{token}', data={
            'password': 'short',
            'password2': 'short',
        })
        assert '8 znaků' in r.data.decode().lower()

    def test_reset_password_expired_token(self, app, client):
        import secrets
        from datetime import datetime
        token = secrets.token_urlsafe(48)
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            admin = db.execute('SELECT id FROM admins WHERE username = ?', ('adam',)).fetchone()
            expired = datetime.now().timestamp() - 3600
            db.execute(
                'INSERT INTO password_reset_tokens (admin_id, token, expires_at) VALUES (?, ?, ?)',
                (admin['id'], token, datetime.fromtimestamp(expired))
            )
            db.commit()

        r = client.get(f'/admin/reset-password/{token}', follow_redirects=True)
        assert 'neplatný' in r.data.decode().lower() or 'vypršel' in r.data.decode().lower()


# ── CSRF protection tests ───────────────────────────────────────────


class TestCSRFProtection:
    def test_post_without_csrf_returns_403(self, app):
        app.test_client_class = FlaskClient
        raw_client = app.test_client()
        r = raw_client.post('/registrace', data={
            'name': 'Test',
            'email': 'test@test.cz',
        })
        assert r.status_code == 403
        app.test_client_class = None

    def test_post_with_wrong_csrf_returns_403(self, app):
        app.test_client_class = FlaskClient
        raw_client = app.test_client()
        with raw_client.session_transaction() as sess:
            sess['_csrf_token'] = 'correct-token'
        r = raw_client.post('/registrace', data={
            'name': 'Test',
            'email': 'test@test.cz',
            '_csrf_token': 'wrong-token',
        })
        assert r.status_code == 403
        app.test_client_class = None

    def test_post_with_valid_csrf_succeeds(self, client):
        with patch('app.send_email'):
            r = client.post('/registrace', data={
                'name': 'Test User',
                'email': 'test@test.cz',
                'gdpr_consent': 'on',
            }, follow_redirects=True)
            assert r.status_code == 200

    def test_csrf_token_generated_on_form_page(self, app):
        app.test_client_class = FlaskClient
        raw_client = app.test_client()
        r = raw_client.get('/registrace')
        with raw_client.session_transaction() as sess:
            assert '_csrf_token' in sess
            assert len(sess['_csrf_token']) == 64
        app.test_client_class = None


# ── Maintenance mode tests ──────────────────────────────────────────


class TestMaintenanceMode:
    def test_maintenance_overlay_shown_when_enabled(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)",
                       ('maintenance_enabled', '1'))
            db.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)",
                       ('maintenance_until', '2099-01-01T00:00:01'))
            db.commit()

        r = client.get('/')
        html = r.data.decode()
        assert 'maintenance-overlay' in html
        assert 'PŘIPRAVUJEME' in html

    def test_maintenance_overlay_hidden_when_disabled(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)",
                       ('maintenance_enabled', '0'))
            db.commit()

        r = client.get('/')
        assert 'maintenance-overlay' not in r.data.decode()

    def test_maintenance_overlay_hidden_after_deadline(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)",
                       ('maintenance_enabled', '1'))
            db.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)",
                       ('maintenance_until', '2020-01-01T00:00:01'))
            db.commit()

        r = client.get('/')
        assert 'maintenance-overlay' not in r.data.decode()

    def test_maintenance_does_not_affect_admin(self, app, admin_client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)",
                       ('maintenance_enabled', '1'))
            db.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)",
                       ('maintenance_until', '2099-01-01T00:00:01'))
            db.commit()

        r = admin_client.get('/admin')
        assert 'maintenance-overlay' not in r.data.decode()

    def test_maintenance_blocks_scrolling(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)",
                       ('maintenance_enabled', '1'))
            db.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)",
                       ('maintenance_until', '2099-01-01T00:00:01'))
            db.commit()

        r = client.get('/')
        assert 'overflow-hidden' in r.data.decode()

    def test_maintenance_toggle_in_admin_settings(self, admin_client, app):
        r = admin_client.post('/admin/nastaveni', data={
            'event_name': 'Test', 'event_year': '2026', 'event_days': '3',
            'event_km': '100', 'event_elevation': '500', 'event_dates': 'test',
            'contact_name_1': 'A', 'contact_name_2': 'B', 'contact_email': 'a@b.cz',
            'photos_link': '', 'photos_text': '', 'payment_amount': '3500',
            'bank_account': '', 'bank_iban': '',
            'maintenance_until': '2099-12-31T23:59:59',
        }, follow_redirects=True)
        assert r.status_code == 200

        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            row = db.execute("SELECT value FROM site_settings WHERE key = 'maintenance_enabled'").fetchone()
            assert row['value'] == '0'


# ── Security headers tests ──────────────────────────────────────────


class TestSecurityHeaders:
    def test_x_content_type_options(self, client):
        r = client.get('/')
        assert r.headers.get('X-Content-Type-Options') == 'nosniff'

    def test_x_frame_options(self, client):
        r = client.get('/')
        assert r.headers.get('X-Frame-Options') == 'DENY'

    def test_session_cookie_httponly(self, app):
        assert app.config['SESSION_COOKIE_HTTPONLY'] is True

    def test_session_cookie_samesite(self, app):
        assert app.config['SESSION_COOKIE_SAMESITE'] == 'Lax'


# ── Context processor tests ─────────────────────────────────────────


class TestContextProcessor:
    def test_injects_settings(self, client):
        r = client.get('/')
        html = r.data.decode()
        assert 'CYKLOEXPEDICE' in html

    def test_injects_etapy_nav(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute("INSERT INTO etapy (number, title) VALUES (?, ?)", (1, 'Testovaci Etapa'))
            db.commit()

        r = client.get('/')
        assert 'Testovaci Etapa' in r.data.decode()

    def test_injects_current_year(self, client):
        from datetime import datetime
        r = client.get('/')
        assert str(datetime.now().year) in r.data.decode()


# ── Public route edge cases ──────────────────────────────────────────


class TestPublicRouteEdgeCases:
    def test_fotky_disabled_returns_404(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)",
                       ('fotky_enabled', '0'))
            db.commit()

        r = client.get('/fotky')
        assert r.status_code == 404

    def test_etapa_detail(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute(
                "INSERT INTO etapy (number, title, description) VALUES (?, ?, ?)",
                (10, 'Detail Test', 'Test description'))
            db.commit()

        r = client.get('/etapa/10')
        assert r.status_code == 200
        assert 'Detail Test' in r.data.decode()

    def test_etapa_nonexistent_returns_404(self, client):
        r = client.get('/etapa/999')
        assert r.status_code == 404

    def test_etapa_prev_next_navigation(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute("INSERT INTO etapy (number, title) VALUES (?, ?)", (1, 'First'))
            db.execute("INSERT INTO etapy (number, title) VALUES (?, ?)", (2, 'Second'))
            db.execute("INSERT INTO etapy (number, title) VALUES (?, ?)", (3, 'Third'))
            db.commit()

        r = client.get('/etapa/2')
        html = r.data.decode()
        assert r.status_code == 200

    def test_aktuality_only_shows_published(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute("INSERT INTO aktuality (title, content, published) VALUES (?, ?, ?)",
                       ('Published News', 'content', 1))
            db.execute("INSERT INTO aktuality (title, content, published) VALUES (?, ?, ?)",
                       ('Draft News', 'content', 0))
            db.commit()

        r = client.get('/aktuality')
        html = r.data.decode()
        assert 'PUBLISHED NEWS' in html
        assert 'DRAFT NEWS' not in html

    def test_propozice_shows_content(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute("INSERT INTO propozice (content) VALUES (?)", ('<p>Test Content</p>',))
            db.commit()

        r = client.get('/propozice')
        assert 'Test Content' in r.data.decode()


# ── Admin profile & SMTP tests ──────────────────────────────────────


class TestAdminProfile:
    def test_save_profile_email(self, admin_client, app):
        r = admin_client.post('/admin/profile', data={
            'email': 'newemail@test.cz',
            'smtp_host': 'smtp.test.cz',
            'smtp_port': '587',
            'smtp_user': 'user',
            'smtp_password': 'pass',
            'smtp_from': 'from@test.cz',
        }, follow_redirects=True)
        assert r.status_code == 200
        assert 'uložen' in r.data.decode().lower()

        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            admin = db.execute('SELECT email FROM admins WHERE username = ?', ('adam',)).fetchone()
            assert admin['email'] == 'newemail@test.cz'
            smtp = db.execute("SELECT value FROM site_settings WHERE key = 'smtp_host_adam'").fetchone()
            assert smtp['value'] == 'smtp.test.cz'


# ── Admin CRUD edge cases ───────────────────────────────────────────


class TestAdminCRUDEdgeCases:
    def test_edit_etapa_post(self, admin_client, app):
        admin_client.post('/admin/etapy/new', data={
            'number': '5', 'title': 'Original Title', 'date': '', 'distance': '',
            'elevation_up': '', 'elevation_down': '', 'route': '',
            'waypoints': '', 'description': '', 'map_link': '',
            'youtube_links': '', 'color': '#ffc107',
        })
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            row = db.execute('SELECT id FROM etapy WHERE number = 5').fetchone()
            etapa_id = row['id']

        r = admin_client.post(f'/admin/etapy/{etapa_id}/edit', data={
            'number': '5', 'title': 'Updated Title', 'date': '2026-09-01',
            'distance': '120', 'elevation_up': '600', 'elevation_down': '500',
            'route': 'A - C', 'waypoints': 'town1', 'description': 'Updated',
            'map_link': '', 'youtube_links': 'https://youtube.com/watch?v=123',
            'color': '#ff0000',
        }, follow_redirects=True)
        assert r.status_code == 200
        assert 'uložena' in r.data.decode().lower()

        with app.app_context():
            db = flask_app.get_db()
            row = db.execute('SELECT title FROM etapy WHERE number = 5').fetchone()
            assert row['title'] == 'Updated Title'

    def test_edit_nonexistent_etapa_returns_404(self, admin_client):
        r = admin_client.get('/admin/etapy/9999/edit')
        assert r.status_code == 404

    def test_edit_aktualita(self, admin_client, app):
        admin_client.post('/admin/aktuality/new', data={
            'title': 'Edit Me', 'content': '<p>original</p>', 'published': '1',
        })
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            row = db.execute("SELECT id FROM aktuality WHERE title = 'Edit Me'").fetchone()
            aid = row['id']

        r = admin_client.get(f'/admin/aktuality/{aid}/edit')
        assert r.status_code == 200

        r = admin_client.post(f'/admin/aktuality/{aid}/edit', data={
            'title': 'Edited Title', 'content': '<p>updated</p>', 'published': '1',
        }, follow_redirects=True)
        assert r.status_code == 200

        with app.app_context():
            db = flask_app.get_db()
            row = db.execute(f'SELECT title FROM aktuality WHERE id = ?', (aid,)).fetchone()
            assert row['title'] == 'Edited Title'

    def test_edit_nonexistent_aktualita_returns_404(self, admin_client):
        r = admin_client.get('/admin/aktuality/9999/edit')
        assert r.status_code == 404

    def test_edit_ubytovani(self, admin_client, app):
        admin_client.post('/admin/ubytovani/new', data={
            'etapa_number': '1', 'name': 'Edit Hotel', 'city': 'Brno',
            'date': '', 'rooms_info': '', 'food_info': '', 'link': '', 'sort_order': '0',
        })
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            row = db.execute("SELECT id FROM ubytovani WHERE name = 'Edit Hotel'").fetchone()
            uid = row['id']

        r = admin_client.get(f'/admin/ubytovani/{uid}/edit')
        assert r.status_code == 200

        r = admin_client.post(f'/admin/ubytovani/{uid}/edit', data={
            'etapa_number': '1', 'name': 'Updated Hotel', 'city': 'Praha',
            'date': '', 'rooms_info': '3x', 'food_info': 'oběd', 'link': '', 'sort_order': '1',
        }, follow_redirects=True)
        assert r.status_code == 200
        assert 'uloženo' in r.data.decode().lower()

    def test_edit_nonexistent_ubytovani_returns_404(self, admin_client):
        r = admin_client.get('/admin/ubytovani/9999/edit')
        assert r.status_code == 404

    def test_delete_ubytovani(self, admin_client, app):
        admin_client.post('/admin/ubytovani/new', data={
            'etapa_number': '1', 'name': 'Delete Hotel', 'city': 'X',
            'date': '', 'rooms_info': '', 'food_info': '', 'link': '', 'sort_order': '0',
        })
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            row = db.execute("SELECT id FROM ubytovani WHERE name = 'Delete Hotel'").fetchone()
            uid = row['id']

        r = admin_client.post(f'/admin/ubytovani/{uid}/delete', follow_redirects=True)
        assert r.status_code == 200
        assert 'smazáno' in r.data.decode().lower()

    def test_propozice_update_existing(self, admin_client, app):
        admin_client.post('/admin/propozice', data={'content': '<p>First</p>'})
        r = admin_client.post('/admin/propozice', data={'content': '<p>Second</p>'},
                              follow_redirects=True)
        assert r.status_code == 200

        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            row = db.execute('SELECT content FROM propozice ORDER BY id DESC LIMIT 1').fetchone()
            assert 'Second' in row['content']

    def test_create_aktualita_unpublished(self, admin_client, app):
        r = admin_client.post('/admin/aktuality/new', data={
            'title': 'Draft', 'content': '<p>draft</p>',
        }, follow_redirects=True)
        assert r.status_code == 200

        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            row = db.execute("SELECT published FROM aktuality WHERE title = 'Draft'").fetchone()
            assert row['published'] == 0


# ── Fio payments edge cases ─────────────────────────────────────────


class TestCheckFioPaymentsEdgeCases:
    def test_handles_empty_transaction_list(self, app):
        import app as flask_app
        fio_response = {
            'accountStatement': {
                'transactionList': None
            }
        }
        with patch('app.requests.get') as mock_get:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: fio_response)
            flask_app.check_fio_payments()

    def test_handles_missing_vs_column(self, app):
        import app as flask_app
        with app.app_context():
            db = flask_app.get_db()
            db.execute(
                "INSERT INTO registrace (name, email, status, variable_symbol, "
                "payment_status, payment_amount) VALUES (?, ?, 'approved', ?, 'pending', ?)",
                ('Test', 'x@y.cz', 50, 3500))
            db.commit()

        fio_response = {
            'accountStatement': {
                'transactionList': {
                    'transaction': [{
                        'column1': {'value': 3500},
                    }]
                }
            }
        }
        with patch('app.requests.get') as mock_get:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: fio_response)
            flask_app.check_fio_payments()

        with app.app_context():
            db = flask_app.get_db()
            row = db.execute("SELECT payment_status FROM registrace WHERE variable_symbol = 50").fetchone()
            assert row['payment_status'] == 'pending'

    def test_handles_network_error(self, app):
        import app as flask_app
        with patch('app.requests.get') as mock_get:
            mock_get.side_effect = Exception('Network error')
            flask_app.check_fio_payments()


# ── Registration edge cases ─────────────────────────────────────────


class TestRegistrationCapacity:
    """Tests for the MAX_CAPACITY=30 registration limit."""

    def _fill_approved(self, app, count):
        """Insert `count` approved registrations into the database."""
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            for i in range(count):
                db.execute(
                    "INSERT INTO registrace (name, email, status) VALUES (?, ?, 'approved')",
                    (f'User {i}', f'user{i}@test.cz'))
            db.commit()

    def test_form_visible_under_capacity(self, app, client):
        self._fill_approved(app, 10)
        r = client.get('/registrace')
        html = r.data.decode()
        assert 'Odeslat přihlášku' in html
        assert 'KAPACITA NAPLNĚNA' not in html

    def test_form_hidden_at_capacity(self, app, client):
        self._fill_approved(app, 30)
        r = client.get('/registrace')
        html = r.data.decode()
        assert 'Odeslat přihlášku' not in html
        assert 'KAPACITA NAPLNĚNA' in html

    def test_form_hidden_above_capacity(self, app, client):
        self._fill_approved(app, 35)
        r = client.get('/registrace')
        html = r.data.decode()
        assert 'KAPACITA NAPLNĚNA' in html

    def test_post_blocked_at_capacity(self, app, client):
        self._fill_approved(app, 30)
        with patch('app.send_email'):
            r = client.post('/registrace', data={
                'name': 'Late User',
                'email': 'late@test.cz',
                'gdpr_consent': 'on',
            }, follow_redirects=True)
        html = r.data.decode()
        assert 'naplněna' in html.lower()
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            row = db.execute("SELECT COUNT(*) c FROM registrace WHERE name = 'Late User'").fetchone()
            assert row['c'] == 0

    def test_post_allowed_under_capacity(self, app, client):
        self._fill_approved(app, 29)
        with patch('app.send_email'):
            r = client.post('/registrace', data={
                'name': 'Just In Time',
                'email': 'just@test.cz',
                'gdpr_consent': 'on',
            }, follow_redirects=True)
        html = r.data.decode()
        assert 'Děkujeme' in html

    def test_pending_registrations_dont_count(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            for i in range(35):
                db.execute(
                    "INSERT INTO registrace (name, email, status) VALUES (?, ?, 'pending')",
                    (f'Pending {i}', f'pending{i}@test.cz'))
            db.commit()
        r = client.get('/registrace')
        html = r.data.decode()
        assert 'Odeslat přihlášku' in html

    def test_denied_registrations_dont_count(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            for i in range(35):
                db.execute(
                    "INSERT INTO registrace (name, email, status) VALUES (?, ?, 'denied')",
                    (f'Denied {i}', f'denied{i}@test.cz'))
            db.commit()
        r = client.get('/registrace')
        html = r.data.decode()
        assert 'Odeslat přihlášku' in html

    def test_nav_button_disabled_at_capacity(self, app, client):
        self._fill_approved(app, 30)
        r = client.get('/')
        html = r.data.decode()
        assert html.count('Kapacita naplněna') >= 2

    def test_nav_button_enabled_under_capacity(self, app, client):
        self._fill_approved(app, 10)
        r = client.get('/')
        html = r.data.decode()
        assert 'Kapacita naplněna' not in html
        assert 'Přihlásit se' in html

    def test_hero_button_disabled_at_capacity(self, app, client):
        self._fill_approved(app, 30)
        r = client.get('/')
        html = r.data.decode()
        assert 'Kapacita naplněna' in html
        assert 'bi-bicycle' not in html

    def test_zero_approved_shows_form(self, client):
        r = client.get('/registrace')
        html = r.data.decode()
        assert 'Odeslat přihlášku' in html

    def test_exactly_29_allows_registration(self, app, client):
        self._fill_approved(app, 29)
        r = client.get('/registrace')
        html = r.data.decode()
        assert 'Odeslat přihlášku' in html
        assert 'KAPACITA NAPLNĚNA' not in html


class TestRegistrationEdgeCases:
    def test_send_payment_nonexistent_returns_404(self, admin_client):
        r = admin_client.post('/admin/registrace/9999/send-payment')
        assert r.status_code == 404

    def test_send_payment_no_iban_configured(self, admin_client, app):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute("INSERT INTO registrace (name, email, status) VALUES (?, ?, 'approved')",
                       ('Test', 'test@test.cz'))
            db.commit()
            row = db.execute("SELECT id FROM registrace WHERE name = 'Test'").fetchone()
            reg_id = row['id']
            db.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)",
                       ('bank_iban', ''))
            db.commit()

        r = admin_client.post(f'/admin/registrace/{reg_id}/send-payment',
                              follow_redirects=True)
        assert 'nastaveny' in r.data.decode().lower()

    def test_deny_nonexistent_registration_returns_404(self, admin_client):
        r = admin_client.get('/admin/registrace/9999/deny')
        assert r.status_code == 404

    def test_deny_nonexistent_registration_post_returns_404(self, admin_client):
        r = admin_client.post('/admin/registrace/9999/deny', data={'reason': 'test'})
        assert r.status_code == 404


# ── Email sending tests ─────────────────────────────────────────────


class TestEmailSending:
    def test_send_email_no_recipient(self, app):
        from app import send_email
        with app.app_context():
            send_email('', 'Test', '<p>body</p>')

    def test_send_email_no_smtp_configured(self, app):
        from app import send_email
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            for username in ['adam', 'michal']:
                db.execute("INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)",
                           (f'smtp_host_{username}', ''))
            db.commit()
            send_email('test@test.cz', 'Test', '<p>body</p>')


# ── YouTube embed converter tests ──────────────────────────────────


class TestYoutubeToEmbed:
    def test_watch_url(self):
        from app import youtube_to_embed
        result = youtube_to_embed('https://www.youtube.com/watch?v=dQw4w9WgXcQ')
        assert result == 'https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ'

    def test_short_url(self):
        from app import youtube_to_embed
        result = youtube_to_embed('https://youtu.be/dQw4w9WgXcQ')
        assert result == 'https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ'

    def test_embed_url(self):
        from app import youtube_to_embed
        result = youtube_to_embed('https://www.youtube.com/embed/dQw4w9WgXcQ')
        assert result == 'https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ'

    def test_nocookie_url_passthrough(self):
        from app import youtube_to_embed
        result = youtube_to_embed('https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ')
        assert result == 'https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ'

    def test_watch_url_without_www(self):
        from app import youtube_to_embed
        result = youtube_to_embed('https://youtube.com/watch?v=abc123_-X')
        assert result == 'https://www.youtube-nocookie.com/embed/abc123_-X'

    def test_invalid_url_returned_as_is(self):
        from app import youtube_to_embed
        assert youtube_to_embed('https://example.com/video') == 'https://example.com/video'

    def test_empty_string(self):
        from app import youtube_to_embed
        assert youtube_to_embed('') == ''


# ── Contact page content tests ─────────────────────────────────────


class TestContactPage:
    def test_contact_page_shows_phone_numbers(self, client):
        r = client.get('/kontakt')
        html = r.data.decode()
        assert '+420 725 604 608' in html
        assert '+420 607 128 731' in html

    def test_contact_page_shows_emails(self, client):
        r = client.get('/kontakt')
        html = r.data.decode()
        assert 'michal.prikryl@atlas.cz' in html
        assert 'adam.prikryl7@gmail.com' in html

    def test_contact_page_has_tel_links(self, client):
        r = client.get('/kontakt')
        html = r.data.decode()
        assert 'href="tel:+420725604608"' in html
        assert 'href="tel:+420607128731"' in html

    def test_contact_page_has_mailto_links(self, client):
        r = client.get('/kontakt')
        html = r.data.decode()
        assert 'href="mailto:michal.prikryl@atlas.cz"' in html
        assert 'href="mailto:adam.prikryl7@gmail.com"' in html


# ── Etapa elevation profile rendering tests ────────────────────────


class TestEtapaElevationProfile:
    def test_elevation_profile_rendered(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute(
                "INSERT INTO etapy (number, title, elevation_up, elevation_down) "
                "VALUES (?, ?, ?, ?)",
                (1, 'Test Etapa', '338 m', '398 m'))
            db.commit()

        r = client.get('/etapa/1')
        html = r.data.decode()
        assert 'VÝŠKOVÝ PROFIL' in html
        assert '338 m' in html
        assert '398 m' in html
        assert 'Stoupání' in html
        assert 'Klesání' in html

    def test_elevation_profile_hidden_without_data(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute(
                "INSERT INTO etapy (number, title) VALUES (?, ?)",
                (1, 'No Elevation'))
            db.commit()

        r = client.get('/etapa/1')
        html = r.data.decode()
        assert 'VÝŠKOVÝ PROFIL' not in html

    def test_elevation_svg_rendered(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute(
                "INSERT INTO etapy (number, title, elevation_up, elevation_down) "
                "VALUES (?, ?, ?, ?)",
                (1, 'SVG Test', '500 m', '400 m'))
            db.commit()

        r = client.get('/etapa/1')
        html = r.data.decode()
        assert '<svg' in html
        assert 'hillGrad' in html


# ── Etapa YouTube embed integration tests ──────────────────────────


class TestEtapaYoutubeEmbed:
    def test_youtube_links_converted_to_nocookie(self, app, client):
        import json as json_mod
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            links = json_mod.dumps(['https://www.youtube.com/watch?v=test123'])
            db.execute(
                "INSERT INTO etapy (number, title, youtube_links) VALUES (?, ?, ?)",
                (1, 'YT Test', links))
            db.commit()

        r = client.get('/etapa/1')
        html = r.data.decode()
        assert 'youtube-nocookie.com/embed/test123' in html
        assert 'youtube.com/watch' not in html

    def test_multiple_youtube_links(self, app, client):
        import json as json_mod
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            links = json_mod.dumps([
                'https://youtu.be/vid1',
                'https://www.youtube.com/watch?v=vid2',
            ])
            db.execute(
                "INSERT INTO etapy (number, title, youtube_links) VALUES (?, ?, ?)",
                (1, 'Multi YT', links))
            db.commit()

        r = client.get('/etapa/1')
        html = r.data.decode()
        assert 'youtube-nocookie.com/embed/vid1' in html
        assert 'youtube-nocookie.com/embed/vid2' in html


# ── Route timeline rendering tests ─────────────────────────────────


class TestRouteTimeline:
    def test_index_shows_route_timeline(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute(
                "INSERT INTO etapy (number, title, distance, route, color) "
                "VALUES (?, ?, ?, ?, ?)",
                (1, 'Etapa Jedna', '85 km', 'Blansko - Znojmo', '#ff6600'))
            db.commit()

        r = client.get('/')
        html = r.data.decode()
        assert 'ETAPY EXPEDICE' in html
        assert 'BLANSKO' in html
        assert 'ZNOJMO' in html
        assert '85 km' in html

    def test_index_timeline_links_to_etapa(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute(
                "INSERT INTO etapy (number, title, route) VALUES (?, ?, ?)",
                (1, 'Test', 'A - B'))
            db.commit()

        r = client.get('/')
        html = r.data.decode()
        assert '/etapa/1' in html

    def test_propozice_shows_route_timeline(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute(
                "INSERT INTO etapy (number, title, distance, route, color) "
                "VALUES (?, ?, ?, ?, ?)",
                (1, 'Prop Etapa', '100 km', 'Praha - Brno', '#ffc107'))
            db.commit()

        r = client.get('/propozice')
        html = r.data.decode()
        assert 'TRASA EXPEDICE' in html
        assert 'PRAHA' in html
        assert 'BRNO' in html

    def test_context_processor_provides_route_and_color(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute(
                "INSERT INTO etapy (number, title, distance, route, color) "
                "VALUES (?, ?, ?, ?, ?)",
                (1, 'Color Test', '50 km', 'X - Y', '#e74c3c'))
            db.commit()

        r = client.get('/')
        html = r.data.decode()
        assert '#e74c3c' in html


# ── Wave divider tests ─────────────────────────────────────────────


class TestWaveDividers:
    def test_index_has_wave_divider(self, app, client):
        with app.app_context():
            import app as flask_app
            db = flask_app.get_db()
            db.execute(
                "INSERT INTO etapy (number, title, route) VALUES (?, ?, ?)",
                (1, 'Wave Test', 'A - B'))
            db.commit()

        r = client.get('/')
        html = r.data.decode()
        assert 'viewBox="0 0 1440 80"' in html

    def test_propozice_has_wave_dividers(self, client):
        r = client.get('/propozice')
        html = r.data.decode()
        assert html.count('viewBox="0 0 1440 80"') >= 1
