# Copyright (c) 2024, efeone and contributors
# For license information, please see license.txt

import frappe
import requests
import json
from frappe.utils import get_datetime, add_to_date
from frappe.model.document import Document
DATE_FORMAT = "%Y-%m-%d"
TIME_FORMAT = "%H:%M:%S.%f"
DATETIME_FORMAT = f"{DATE_FORMAT} {TIME_FORMAT}"


class AmazonSellerCentralSettings(Document):
	def on_update(self):
		if self.enable:
			generate_access_token()

def get_authorisation_token():
	'''
		Method to get authorisation_token from the Settings
	'''
	return frappe.utils.password.get_decrypted_password(
		"Amazon Seller Central Settings", "Amazon Seller Central Settings", "authorisation_token"
	)

def get_access_token():
	'''
		Method to get access_token from the Settings, if exprired generate and return new token
	'''
	if frappe.db.get_single_value("Amazon Seller Central Settings", "access_token"):
		token_exprire_on = frappe.db.get_single_value("Amazon Seller Central Settings", "token_exprire_on")
		if token_exprire_on and get_datetime()> get_datetime(token_exprire_on):
			return frappe.utils.password.get_decrypted_password(
				"Amazon Seller Central Settings", "Amazon Seller Central Settings", "access_token"
			)
	return generate_access_token()

def generate_access_token():
	'''
		Method to generate access_token
	'''
	endpoint = "https://api.amazon.com/auth/o2/token"
	authorisation_token = get_authorisation_token()
	client_id = frappe.db.get_single_value("Amazon Seller Central Settings", "client_id")
	client_secret = frappe.db.get_single_value("Amazon Seller Central Settings", "client_secret")

	response_data = {
		"grant_type": "refresh_token",
		"refresh_token": authorisation_token,
		"client_secret": client_secret,
		"client_id": client_id
	}

	response = requests.post(
		endpoint,
		json=response_data,
		headers={
			"Content-Type": "application/json",
		},
	)
	if response.ok:
		response_json = response.json()
		expires_in = 3600
		if response_json.get('expires_in'):
			expires_in = response_json.get('expires_in')
		if response_json.get('access_token'):
			access_token = response_json.get('access_token')
			token_generated_on = get_datetime()
			token_exprire_on = add_to_date(token_generated_on, seconds=expires_in)
			frappe.db.set_single_value("Amazon Seller Central Settings", "access_token", access_token)
			frappe.db.set_single_value("Amazon Seller Central Settings", "token_generated_on", get_datetime(token_generated_on).strftime(DATETIME_FORMAT))
			frappe.db.set_single_value("Amazon Seller Central Settings", "token_exprire_on", get_datetime(token_exprire_on).strftime(DATETIME_FORMAT))
			return access_token
