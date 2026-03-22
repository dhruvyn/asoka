# Account Rules

Accounts are never deleted. To retire a customer, set the Type field to "Churned". Deleting an Account record is not permitted under any circumstances.

Account_Tier__c (Enterprise, Mid-Market, SMB) defines the service level for the account. Enterprise accounts receive priority handling. Changes to Account_Tier__c should be noted in the batch summary.

Is_Strategic__c is a checkbox that flags key accounts requiring special handling. Strategic accounts may have exceptions to standard policies. When Is_Strategic__c is true, escalate any destructive or sensitive operations to the human coworker for review.

Payment_Terms__c must be set on all Accounts where Type is "Customer". Valid values are: Net 15, Net 30, Net 45, Net 60, Due Upon Receipt. Do not leave Payment_Terms__c blank on Customer accounts.

Account Type transitions are one-directional: Prospect → Customer → Churned. Never move an Account backwards (e.g., from Customer to Prospect or from Churned to Customer) without explicit instruction.

# Opportunity Rules

Discount_Percent__c cannot exceed 30%. This is enforced by a Salesforce validation rule. The bot must never propose a Discount_Percent__c value above 0.30 (30%). If a user requests a discount above 30%, reject the request and explain the cap.

ARR_Legacy__c is DEPRECATED. Do not read from or write to this field under any circumstances. For new business revenue, use ARR_New__c. For expansion revenue, use ARR_Expansion__c. ARR_Legacy__c exists only for historical records and must be ignored in all operations.

ForecastCategory can only be edited by users who have the Forecast_Editor permission set assigned. Do not propose changes to ForecastCategory unless the requesting user has this permission. If unsure, flag it in the batch assumptions.

Opportunity Names follow the convention: [Account Name] - [Type] - [Quarter/Year]. Example: "Acme Corp - Renewal - Q2 2026". When creating a new Opportunity, generate the Name using this convention based on the account name, opportunity type, and current quarter.

ARR_New__c is used for New Business type opportunities. ARR_Expansion__c is used for Expansion type opportunities. Renewal opportunities use ARR_New__c. Never mix these fields across wrong opportunity types.

# Case Rules

Case Subjects follow the naming convention: [Account Name] - [Issue Type]. Example: "Acme Corp - Login Failure". Always follow this convention when creating new Cases.

Priority "Critical" triggers a 2-hour SLA. When setting a Case to Critical priority, note this SLA in the batch summary so the approver is aware of the urgency.

When a Case Status is set to "Escalated", ownership automatically transfers to the Support Manager queue via a Salesforce Flow. Do not manually reassign Case ownership when escalating — the automation handles it. Manually reassigning on an escalation will conflict with the Flow.

Case Status transitions are: New → In Progress → Escalated → Closed. Cases should not skip directly from New to Closed without a resolution note.

# User Deactivation Procedure

When deactivating a user, the following steps must be executed in this exact order. Do not skip or reorder steps.

Step 1: Transfer all Accounts owned by the user (OwnerId = user being deactivated) to the user's direct manager. The manager is identified by the ManagerId field on the User record.

Step 2: Transfer all Opportunities owned by the user to the same direct manager.

Step 3: Transfer all open Cases owned by the user to the same direct manager. Open Cases are those where Status is not "Closed".

Step 4: Set IsActive = false on the User record.

Never set IsActive = false before completing Steps 1 through 3. Deactivating first leaves orphaned records owned by an inactive user, which causes downstream issues in Salesforce reports and assignments.

Always confirm the manager's identity in the batch assumptions: "Transferring to [Manager Name] (User ID: [ID]), the direct manager per ManagerId on the User record."

# Role Hierarchy

The sales role hierarchy is: VP Sales → Sales Manager → Account Executive.

The support role hierarchy is: Support Manager → Support Agent.

These two hierarchies are independent. A Sales Manager does not manage Support Agents and vice versa.

Ownership transfers always go to the user's direct manager (the ManagerId field on the User record), not to their manager's manager. If a user's direct manager is also inactive or missing, flag this as a blocker and do not proceed with deactivation.

# Permission Sets

Forecast_Editor: Required to edit the ForecastCategory field on Opportunity records. If a write plan involves changing ForecastCategory, note in the assumptions that this requires the Forecast_Editor permission set.

API_Access: Required for API-based integrations. Standard users without this permission set cannot access Salesforce via the API. Not relevant to the bot's own operations.

# Field Usage Notes

Opportunity.Description is for internal notes only. It is never shown to customers. It is safe to update.

Account.Type valid transitions are Prospect → Customer → Churned only. Do not reverse this flow.

Use ARR_New__c for new business and renewals. Use ARR_Expansion__c for expansions. Never use ARR_Legacy__c.

Case.OwnerId for escalated cases is managed by a Salesforce Flow — do not override it manually during an escalation operation.

User.IsActive = false is the correct way to deactivate a user. Do not attempt to delete User records.
