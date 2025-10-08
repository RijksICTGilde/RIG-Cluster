# Application Database Pool Lifecycle

This document describes the complete lifecycle of database pools in the Operations Manager application.

## Startup Sequence (opi/server.py)

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Print boot banner
    print_boot_banner()
    
    # 2. Initialize database connection pools
    await initialize_database_pools()  # ✅ ADDED
    logger.info("Database pools initialized successfully")
    
    # 3. Run startup tasks (namespace creation, SOPS secrets, etc.)
    await run_startup_tasks()
    
    # 4. Start Git monitoring service
    await start_git_monitoring(app)
    
    # Application runs...
    yield
    
    # 5. Stop Git monitoring service
    await stop_git_monitoring()
    
    # 6. Close database connection pools
    await close_database_pools()  # ✅ ADDED
    logger.info("Database pools closed successfully")
    
    # 7. Shutdown logging
    logging.shutdown()
```

## Database Pool Architecture

### 1. Pool Initialization (`initialize_database_pools()`)
- Creates `DatabasePool` instances for each database configuration
- Initializes asyncpg connection pools
- Stores pools in global registry by name
- Handles connection failures gracefully

### 2. Pool Usage (Throughout Application)
```python
# Get pool from registry
main_pool = get_database_pool("main")

# Inject into components
db_manager = DatabaseManager(project_manager, main_pool)
```

### 3. Pool Cleanup (`close_database_pools()`)
- Closes all asyncpg connection pools
- Releases all active connections
- Clears the global pool registry
- Handles cleanup errors gracefully

## Benefits of This Lifecycle

✅ **Connection Management**: Pools are properly initialized and cleaned up  
✅ **Resource Cleanup**: No connection leaks on application shutdown  
✅ **Graceful Degradation**: Application can start even if some pools fail  
✅ **Proper Ordering**: Database pools initialized before they're needed  
✅ **Error Handling**: Pool failures don't crash the application  

## Race Condition Solution

The original race condition is eliminated because:

1. **Single Pool**: All database operations use connections from the same pool
2. **Single Connection**: Each `DatabaseManager` gets one connection for all related operations
3. **Transaction Consistency**: User creation and database creation use the same connection
4. **Connection Reuse**: Pool provides efficient connection management

## Usage Example

```python
# Application startup automatically initializes pools

# In your code, get a pool and use it
main_pool = get_database_pool("main")
db_manager = DatabaseManager(project_manager, main_pool)

# Operations use the same connection - no race condition!
await db_manager.create_resources_for_deployment(project_data, deployment)

# Application shutdown automatically closes pools
```

The database connection architecture is now production-ready with proper lifecycle management! 🎉