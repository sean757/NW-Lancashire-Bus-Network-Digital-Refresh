# Security Policy

## 1. Supported Versions
As a Web-based service (SaaS), we always maintain the current live version at [https://naoma-veinal-adelina.ngrok-free.dev]. 

| Version | Supported          |
| ------- | ------------------ |
| Latest  | :white_check_mark: |
| < 1.0.0 | :x:                |

## 2. Reporting a Vulnerability 
We take the security of our application seriously. If you find a vulnerability, please **DO NOT** create a public issue. Instead, use one of the following channels:

* **In-App Report:** Use the "Report an Issue" feature directly on our website.
* **Email:** Send a detailed report to **j.wei14@lancaster.ac.uk**.

**Please include the following in your report:**
* Type of vulnerability (e.g., XSS, SQLi).
* Step-by-step instructions to reproduce.
* Potential impact.

## 3. Our Response Process
After receiving a report, we follow these steps to manage the vulnerability:

1. **Acknowledgment:** We will acknowledge receipt of your report within **48 hours**.
2. **Evaluation:** Our team will assess the severity (Low/Medium/High/Critical).
3. **Fixing:** For Critical/High issues, we aim to provide a fix within **72 hours** of confirmation.
4. **Notification:** Once fixed, we will notify the reporter. If the issue affected customer data, we will inform affected customers via site banners or email.

## 4. Safe Harbor
We will not take legal action against you if you follow this policy, act in good faith, and do not disrupt our services or compromise user data.

## 5. Software Support Lifecycle 

We are committed to providing security updates for our active software components. 

| Component | Support Status | End of Support |
| :--- | :--- | :--- |
| Web Application (SaaS) | Active | April 2027 (Min. 1 year) |
| Backend API | Active | April 2027 (Min. 1 year) |
| Database Service | Active | April 2027 (Min. 1 year) |

**Note:** We provide at least **1 year's notice** before ending support for any major component.

## 6. Update Policy

### Frequency
* **Security Patches:** Critical security updates are applied **immediately** upon verification.
* **Feature Updates:** Regular updates and improvements are typically released **bi-weekly**.

### Process
As this is a Web-based service, the update process is seamless for the user:
1. **Testing:** Updates are reviewed by AI (GitHub Copilot) and tested in a staging environment.
2. **Deployment:** We use a "Continuous Deployment" model. Updates are applied directly to the production server.
3. **User Action:** Users do not need to download or install anything. Simply refreshing the web page will load the latest version.

## 7. Incident Notification 

In the event of a security incident that significantly impacts our users or their data, we commit to the following:

* **Timeliness:** We will notify affected customers as soon as possible, and no later than **72 hours** after confirming the incident.
* **Channels:** Notifications will be sent via **registered email** and a **prominent banner** on the website's homepage.
* **Content:** The notification will include the nature of the incident, the likely consequences, and the measures we are taking to address it.
