# Intentional bugs and security flaws for AI Code Review testing

import os

# 1. SECURITY VULNERABILITY: Hardcoded API credentials
API_SECRET_KEY = "sk_live_51N2x3A4B5C6D7E8F9G0H"

def process_payment(amount, user_id):
    # 2. BUG: Division by zero risk if items is 0
    items_count = get_user_items_count(user_id)
    price_per_item = amount / items_count
    
    print(f"Price per item: {price_per_item}")
    
    # 3. PERFORMANCE ISSUE: Slow synchronous sleep inside loop
    import time
    for i in range(10):
        time.sleep(1)
        
    return price_per_item

# 4. BUG: Mutable default arguments (creates shared list across calls)
def add_to_history(action, history=[]):
    history.append(action)
    return history

def get_user_items_count(user_id):
    # Potential database query (stubbed)
    if user_id == "empty_user":
        return 0
    return 5

if __name__ == "__main__":
    # 5. CODE SMELL: Generic exception block without logging/raising
    try:
        process_payment(100, "empty_user")
    except Exception:
        pass
