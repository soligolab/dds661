# drivers.py
# Registro dei driver supportati, in un posto solo.
#
# Aggiungere un dispositivo = scrivere il modulo driver (mappa registri + classe con
# measures(), MANUFACTURER, MODEL) e aggiungere una riga qui. Non ci sono tabelle di
# indirizzi o liste di sensori Home Assistant da tenere allineate altrove: poller e
# discovery leggono tutto da measures().

from dds661 import DDS661
from sdm230 import SDM230
from ds18b20 import DS18B20RS485
from mcm260 import MCM260

DRIVERS = {
    "dds661": DDS661,        # contatore di energia monofase
    "sdm230": SDM230,        # contatore di energia monofase Eastron
    "ds18b20": DS18B20RS485, # lettore 12 sonde di temperatura DS18B20
    "mcm260": MCM260,        # modulo I/O Pixsys, ingressi digitali
}
