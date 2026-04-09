todo-ish:
op het moment staan de 2 test script [provision_apm_user.py](provision_apm_user.py) en [test_apm_lib.py](test_apm_lib.py) in de root van de repo, de test moet probably weg maar de provision moet naar het operation-manager-python gedeelte. 


in het provision script wordt nu infrastructure/bootstrap/infrastructure/elastic/controller/base/apm_server_template.yaml.tmp aangeroepen en aangevuld met de benodigde variabelen.
deze moet misschien verhuist worden naar operations-manager/python/manifests ?



random andere dingen:
in infrastructure/bootstrap/infrastructure/postgresql/database/base/databases.yaml heb ik de operations-manager-db toegevoegd. Bij mij werd deze niet aangemaakt door het python script, misschien timing issue?
taskfile regel 2217 change komt van robbert u zelf, dit is omdat sed anders werkt op linux dan op mac