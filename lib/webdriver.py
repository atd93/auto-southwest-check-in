from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sbvirtualdisplay import Display
from seleniumbase import Driver
from seleniumbase.fixtures import page_actions as seleniumbase_actions

from .config import IS_DOCKER
from .log import LOGS_DIRECTORY, get_logger
from .utils import DriverTimeoutError, LoginError, random_sleep_duration

if TYPE_CHECKING:
    from .checkin_scheduler import CheckInScheduler
    from .reservation_monitor import AccountMonitor

BASE_URL = "https://mobile.southwest.com"
CHECKIN_URL = BASE_URL + "/air/check-in/"
LOGIN_URL = BASE_URL + "/api/security/v4/security/token"
TRIPS_URL = BASE_URL + "/api/mobile-misc/v1/mobile-misc/page/upcoming-trips"
HEADERS_URL = BASE_URL + "/api/mobile-air-booking/v1/mobile-air-booking/feature/shopping-details"

# Southwest's code when logging in with the incorrect information
INVALID_CREDENTIALS_CODE = 400518024

WAIT_TIMEOUT_SECS = 180

JSON = dict[str, Any]

logger = get_logger(__name__)


class WebDriver:
    """
    Controls fetching valid headers for use with the Southwest API.

    This class can be instantiated in two ways:
    1. Setting/refreshing headers before a check-in to ensure the headers are valid. The
    check-in URL is requested in the browser. One of the requests from this initial request
    contains valid headers which are then set for the CheckIn Scheduler.

    2. Logging into an account. In this case, the headers are refreshed and a list of scheduled
    flights are retrieved.

    Some of this code is based off of:
    https://github.com/byalextran/southwest-headers/commit/d2969306edb0976290bfa256d41badcc9698f6ed
    """

    def __init__(self, checkin_scheduler: CheckInScheduler) -> None:
        self.checkin_scheduler = checkin_scheduler
        self.headers_set = False
        self.debug_screenshots = self._should_take_screenshots()
        self.display = None

        # For account login
        self.login_request_id = None
        self.login_status_code = None
        self.trips_request_id = None

    def _should_take_screenshots(self) -> bool:
        """
        Determines if the webdriver should take screenshots for debugging based on the CLI arguments
        of the script. Similarly to setting verbose logs, this cannot be kept track of easily in a
        global variable due to the script's use of multiprocessing.
        """
        arguments = sys.argv[1:]
        if "--debug-screenshots" in arguments:
            logger.debug("Taking debug screenshots")
            return True

        return False

    def _take_debug_screenshot(self, driver: Driver, name: str) -> None:
        """Take a screenshot of the browser and save the image as 'name' in LOGS_DIRECTORY"""
        if self.debug_screenshots:
            driver.save_screenshot(Path(LOGS_DIRECTORY) / name)

    def set_headers(self) -> None:
        """
        The check-in URL is requested. Since another request contains valid headers
        during the initial request, those headers are set in the CheckIn Scheduler.
        """
        driver = self._get_driver()
        self._take_debug_screenshot(driver, "pre_headers.png")
        logger.debug("Waiting for valid headers")
        # Once this attribute is set, the headers have been set in the checkin_scheduler
        self._wait_for_attribute("headers_set")
        self._take_debug_screenshot(driver, "post_headers.png")

        self._quit_driver(driver)

    def get_reservations(self, account_monitor: AccountMonitor) -> list[JSON]:
        """
        Logs into the account being monitored to retrieve a list of reservations. Since
        valid headers are produced, they are also grabbed and updated in the check-in scheduler.
        Last, if the account name is not set, it will be set based on the response information.
        """
        driver = self._get_driver()
        driver.add_cdp_listener("Network.responseReceived", self._login_listener)

        logger.debug("Logging into account to get a list of reservations and valid headers")

        # Log in to retrieve the account's reservations and needed headers for later requests
        seleniumbase_actions.wait_for_element_not_visible(driver, ".dimmer")
        self._take_debug_screenshot(driver, "pre_login.png")

        # If a popup came up with an error, click "OK" to remove it.
        # See https://github.com/jdholtz/auto-southwest-check-in/issues/226
        driver.click_if_visible(".button-popup.confirm-button")

        driver.click(".login-button--box")
        time.sleep(random_sleep_duration(1, 5))
        driver.type('input[name="userNameOrAccountNumber"]', account_monitor.username)

        # Use quote_plus to workaround a x-www-form-urlencoded encoding bug on the mobile site
        driver.type('input[name="password"]', f"{account_monitor.password}\n")

        # Wait for the necessary information to be set
        self._wait_for_attribute("headers_set")
        self._wait_for_login(driver, account_monitor)
        self._take_debug_screenshot(driver, "post_login.png")

        # The upcoming trips page is also loaded when we log in, so we might as well grab it
        # instead of requesting again later
        reservations = self._fetch_reservations(driver)

        self._quit_driver(driver)
        return reservations

    def _get_driver(self) -> Driver:
        logger.debug("Starting webdriver for current session")
        browser_path = self.checkin_scheduler.reservation_monitor.config.browser_path

        driver_version = "mlatest"
        if IS_DOCKER:
            self._start_display()
            # Make sure a new driver is not downloaded as the Docker image
            # already has the correct driver
            driver_version = "keep"

        self.driver = Driver(
            binary_location=browser_path,
            driver_version=driver_version,
            headed=IS_DOCKER,
            headless=not IS_DOCKER,
            uc_cdp_events=True,
            undetectable=True,
            incognito=True,
        )

        logger.debug("Using browser version: %s", self.driver.caps["browserVersion"])

        #self.driver.add_cdp_listener("Network.requestWillBeSent", self._headers_listener)

        logger.debug("Loading Southwest check-in page (this may take a moment)")
        self.driver.open(CHECKIN_URL)
        self._take_debug_screenshot(self.driver, "after_page_load.png")
        return self.driver

    def _login_listener(self, data: JSON) -> None:
        """
        Wait for various responses that are needed once the account is logged in. The request IDs
        are kept track of to get the response body associated with them later.
        """
        response = data["params"]["response"]
        if response["url"] == LOGIN_URL:
            logger.debug("Login response has been received")
            self.login_request_id = data["params"]["requestId"]
            self.login_status_code = response["status"]
        elif response["url"] == TRIPS_URL:
            logger.debug("Upcoming trips response has been received")
            self.trips_request_id = data["params"]["requestId"]

    def _wait_for_attribute(self, attribute: str) -> None:
        logger.debug("Waiting for %s to be set (timeout: %d seconds)", attribute, WAIT_TIMEOUT_SECS)
        poll_interval = 0.5

        attempts = 0
        max_attempts = WAIT_TIMEOUT_SECS / poll_interval
        while not getattr(self, attribute) and attempts < max_attempts:
            time.sleep(poll_interval)
            attempts += 1

        if attempts >= max_attempts:
            timeout_err = DriverTimeoutError(f"Timeout waiting for the '{attribute}' attribute")
            logger.debug(timeout_err)
            raise timeout_err

        logger.debug("%s set successfully", attribute)

    def _wait_for_login(self, driver: Driver, account_monitor: AccountMonitor) -> None:
        """
        Waits for the login request to go through and sets the account name appropriately.
        Handles login errors, if necessary.
        """
        self._click_login_button(driver)
        self._wait_for_attribute("login_request_id")
        # Manually inject cookies into headers after login completes
        request_headers = {
            "x-api-key": "l7xx2c186c1297274b828b1872e22edfe67a",  # or pull dynamically
            "x-channel-id": "MWEB",
            "user-agent": self.driver.execute_script("return navigator.userAgent;"),
            "accept": "application/json",
            "referer": "https://mobile.southwest.com/air/check-in/",
            "content-type": "application/json",
        }

        # Inject cookie header from current browser session
        cookies = self.driver.get_cookies()
        cookie_header = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
        request_headers["cookie"] = cookie_header
        
        # Save for checkin_scheduler
        self.checkin_scheduler.headers = request_headers
        self.headers_set = True

        login_response = self._get_response_body(driver, self.login_request_id)

        # Handle login errors
        if self.login_status_code != 200:
            self._quit_driver(driver)
            error = self._handle_login_error(login_response)
            raise error

        self._set_account_name(account_monitor, login_response)

    def _click_login_button(self, driver: Driver) -> None:
        """
        In some cases, the submit action on the login form may fail. Therefore, try clicking
        again, if necessary.
        """
        seleniumbase_actions.wait_for_element_not_visible(driver, ".dimmer")
        if driver.is_element_visible("div.popup"):
            # Don't attempt to click the login button again if the submission form went through,
            # yet there was an error
            return

        login_button = "button#login-btn"
        try:
            seleniumbase_actions.wait_for_element_not_visible(driver, login_button, timeout=5)
        except Exception:
            logger.debug("Login form failed to submit. Clicking login button again")
            driver.click(login_button)

    def _fetch_reservations(self, driver: Driver) -> list[JSON]:
        """
        Waits for the reservations request to go through and returns only reservations
        that are flights.
        """
        self._wait_for_attribute("trips_request_id")
        trips_response = self._get_response_body(driver, self.trips_request_id)
        reservations = trips_response["upcomingTripsPage"]
        return [reservation for reservation in reservations if reservation["tripType"] == "FLIGHT"]

    def _get_response_body(self, driver: Driver, request_id: str) -> JSON:
        response = driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": request_id})
        return json.loads(response["body"])

    def _handle_login_error(self, response: JSON) -> LoginError:
        if response.get("code") == INVALID_CREDENTIALS_CODE:
            logger.debug("Invalid credentials provided when attempting to log in")
            reason = "Invalid credentials"
        else:
            logger.debug("Logging in failed for an unknown reason")
            reason = "Unknown"

        return LoginError(reason, self.login_status_code)

    def _get_needed_headers(self, request_headers: JSON) -> JSON:
        headers = dict(request_headers)
    
        try:
            cookies = self.driver.get_cookies() if hasattr(self, "driver") else []
            cookie_header = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
            if cookie_header:
                headers["cookie"] = cookie_header
        except Exception as e:
            logger.debug("Error while extracting cookies: %s", e)
    
        return headers



    def _set_account_name(self, account_monitor: AccountMonitor, response: JSON) -> None:
        if account_monitor.first_name:
            # No need to set the name if this isn't the first time logging in
            return

        logger.debug("First time logging in. Setting account name")
        account_monitor.first_name = response["customers.userInformation.firstName"]
        account_monitor.last_name = response["customers.userInformation.lastName"]

        print(
            f"Successfully logged in to {account_monitor.first_name} "
            f"{account_monitor.last_name}'s account\n"
        )  # Don't log as it contains sensitive information

    def _quit_driver(self, driver: Driver) -> None:
        driver.quit()
        self._stop_display()

    def _start_display(self) -> None:
        try:
            self.display = Display(size=(1440, 1880), backend="xvfb")
            self.display.start()

            if self.display.is_alive():
                logger.debug("Started virtual display successfully")
            else:
                logger.debug("Started virtual display but is not active")
        except Exception as e:
            logger.debug("Failed to start display: %s", e)

    def _stop_display(self) -> None:
        if self.display is not None:
            self.display.stop()
            logger.debug("Stopped virtual display successfully")
