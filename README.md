# Sointu Studio

Oma musiikki- ja äänistudio GPU-palvelimelle. Kaikki toimii yhdessä selainkäyttöliittymässä:

- **Musiikki (ACE-Step 1.5).** Koulutat LoRA-adapterin omasta musiikistasi ja generoit uusia biisejä sen tyyliin.
- **Äänimallit (RVC / Applio).** Koulutat äänimallin omasta laulustasi.
- **Muunnos.** Muutat minkä tahansa biisin tai ACE-näytteen laulun omalle äänellesi. Studio erottelee laulun, muuntaa sen ja miksaa takaisin taustan kanssa.

## Asennus

Palvelimelle (Ubuntu 22.04/24.04, NVIDIA-GPU ja ajurit asennettuna):

```bash
curl -fsSL https://raw.githubusercontent.com/ratam0/sointu-studio/main/install.sh | sudo bash
```

Asennus kestää noin 30–60 minuuttia, koska PyTorch ja mallit vievät yhteensä noin 35 GB. Asennin ei kysy mitään. Lopuksi se tulostaa SSH-tunnelikomennon.

Omalla koneellasi:

```bash
ssh -N -L 7860:127.0.0.1:7860 käyttäjä@palvelimen-ip
```

Sen jälkeen avaa selaimessa **http://localhost:7860**. Studio kuuntelee vain palvelimen sisäistä osoitetta, joten se ei näy internetiin, ja yhteys kulkee salattuna SSH:n läpi.

### Vaatimukset

| | Minimi | Suositus |
|---|---|---|
| GPU | NVIDIA 16 GB | 24 GB (RTX 4090/3090, A10, L4) tai enemmän |
| NVIDIA-ajuri | 570+ (CUDA 12.8) | uusin |
| Levy | 60 GB vapaata | 100 GB+ |
| RAM | 16 GB | 32 GB |
| Järjestelmä | Ubuntu/Debian ja systemd (tavallinen VPS, ei kontti) | |

### Asennuksen valinnat

Valinnat annetaan ympäristömuuttujina:

```bash
curl -fsSL …/install.sh | sudo NO_BASE=1 bash        # ilman ACE-Stepin base-mallia (–5 GB)
curl -fsSL …/install.sh | sudo STUDIO_PORT=8080 bash # eri portti
```

Asennus menee kansioon `/opt/sointu` ja ajetaan palvelukäyttäjänä `studio`. Asentimen voi ajaa uudelleen milloin tahansa: se korjaa ja päivittää asennuksen, eikä koske projekteihin tai malleihin.

## Hallinta: `studio`-komento

```
studio status      palvelu, GPU, levy, versiot
studio logs -f     palvelun loki
studio restart     uudelleenkäynnistys (käynnissä oleva työ keskeytyy)
studio update      hae uusin versio GitHubista ja päivitä
studio tunnel      tulosta SSH-tunnelikomento
studio password    lisäsalasana web-käyttöliittymään (valinnainen)
studio backup      varmuuskopioi projektit, äänimallit ja asetukset
studio doctor      tarkista ympäristöt, GPU ja mallit
```

## Käyttö

### Musiikki – ACE-Step LoRA

1. **Kappaleet.** Lataa 10–50 tyylillisesti yhtenäistä omaa kappaletta ja paina *Analysoi*. Analyysi täyttää tempon ja sävellajin, tunnistaa laulun ja lyriikat Whisperillä ja kirjoittaa tyylikuvauksen ACE-Stepin kielimallilla. Tarkista lyriikat ja lisää rakennemerkinnät (`[Verse]`, `[Chorus]`). Itse muokattuja kenttiä analyysi ei ylikirjoita.
2. **Koulutus.** Valitse pohjamalli (base on laadukkain, turbo nopein) ja epochit; studio ehdottaa sopivaa määrää. Loss-käyrä, arvioitu jäljellä oleva aika ja loki näkyvät oikealla. Esikäsittely tallennetaan välimuistiin, joten uusi ajo samalla datalla alkaa suoraan koulutuksesta.
3. **Kuuntele.** Generoi näytteitä eri checkpointeista ja vertaa niitä. Laukaisusana lisätään kuvaukseen automaattisesti. *Laula omalla äänellä* vie näytteen suoraan muunnokseen.

### Äänimallit – RVC

1. **Data.** Lataa omia lauluäänitteitä: kokonaisia biisejä tai pelkkää laulua. Tavoite on 10–30 minuuttia puhdasta laulua, ja mittari näyttää kertyneen määrän. Jokaiselle tiedostolle voi valita käsittelyt:
   - **Erota taustasta:** BS-Roformer-malli erottaa laulun soittimista. Oletuksena päällä.
   - **Taustalaulut pois:** karaoke-malli jättää vain pääsolistin. Hyödyllinen, jos biisissä on stemmalauluja.
   - **Kaiku pois:** de-reverb-malli kuivaa laulun. Oletuksena päällä, koska kaiku heikentää äänimallia.

   Lopuksi hiljaiset kohdat leikataan pois. Kuuntele tulos *puhdas*-soittimesta.
2. **Koulutus.**
   - Asetukset ovat RMVPE-sävelkorkeus, ContentVec-piirteet ja HiFi-GAN, oletuksena 40 kHz. Epochimäärä ehdotetaan datan määrän perusteella.
   - 24 GB:n GPU:lla koulutus kestää tyypillisesti 20–60 minuuttia.
   - Malli tallennetaan joka N. epoch. *Lähin parasta* merkitsee sen tallennuksen, joka on lähimpänä alinta loss-arvoa.
   - Ylikoulutettu malli alkaa kuulostaa metalliselta, joten vertaa epocheja kuuntelemalla.
   - *Lataa* antaa `.pth`- ja `.index`-tiedoston, jotka toimivat myös Appliossa ja muissa RVC-työkaluissa.

### Muunnos

Valitse lähteeksi ladattu tiedosto tai ACE-näyte ja valitse äänimalli. Säädettävät asetukset:

| Asetus | Vaikutus |
|---|---|
| Sävelkorkeus | Puolisävelaskeleina. Esimerkiksi miesääni naislaulajan biisiin: −12. |
| Indeksin vaikutus | Suurempi kuulostaa enemmän sinulta, mutta voi lisätä artefakteja. |
| Konsonanttien suojaus | Suurempi pitää s-äänet ja hengitykset selkeämpinä. |
| Laulun ja taustan taso | Miksauksen tasapaino. |

Tuloksena saat valmiin miksauksen (MP3 ja FLAC), muunnetun laulun, taustan ja alkuperäisen erotellun laulun.

GPU-työt (analyysi, koulutukset, näytteet, muunnokset) ajetaan jonossa yksi kerrallaan, joten muisti ei lopu kesken.

## Komponentit ja versiot

| Osa | Versio | Lisenssi |
|---|---|---|
| [ACE-Step 1.5](https://github.com/ace-step/ACE-Step-1.5) | commit `ca1e85f` | MIT |
| [Applio](https://github.com/IAHispano/Applio) (RVC) | 3.7.0 | MIT |
| [audio-separator](https://github.com/nomadkaraoke/python-audio-separator) | 0.47.1 | MIT |
| faster-whisper | uusin 1.x | MIT |

Versiot on kiinnitetty tiedostossa `scripts/setup.sh`. Päivitä ne siellä, kun haluat siirtyä uudempaan.

Hakemistorakenne palvelimella:

```
/opt/sointu/
  studio/            web-palvelin (FastAPI) ja työprosessit
  scripts/           setup.sh, start.sh, studio-komento
  vendor/            ACE-Step-1.5 ja Applio omine Python-ympäristöineen
  models/            ACE-mallit, erottelumallit, Whisper
  data/              projektit, äänimallit, muunnokset, lokit
  .env               asetukset (portti, salasana, Whisper-malli)
```

## Vianetsintä

| Ongelma | Ratkaisu |
|---|---|
| Asennus keskeytyy | Katso `/opt/sointu/install.log` ja aja asennus uudelleen. Se jatkaa siitä, mihin jäi. |
| `CUDA out of memory` ACE-koulutuksessa | Valitse muistiprofiiliksi *Säästö* tai pienennä *Max kesto* 180 sekuntiin. |
| `CUDA out of memory` RVC-koulutuksessa | Pienennä batch 4:ään. |
| Äänimalli kuulostaa metalliselta tai suhisevalta | Käytä aiempaa epochia, pienennä indeksin vaikutusta ja nosta suojausta. |
| Äänimallissa on kaikua tai taustamusiikkia | Varmista, että *Erota taustasta* ja *Kaiku pois* ovat päällä, ja valmistele data uudelleen. |
| Työ jäi tilaan *keskeytyi* | Palvelin käynnistyi uudelleen kesken työn. Jatka koulutusta checkpointista. |
| Mikä tahansa muu | Aja `studio doctor`; se näyttää, mikä osa puuttuu. |

## Julkaisu omaan GitHubiin

```bash
cd sointu-studio
scripts/set-repo.sh <github-käyttäjänimesi>     # asettaa osoitteen asennuskomentoon
git init && git add . && git commit -m "Sointu Studio"
git branch -M main
git remote add origin https://github.com/<github-käyttäjänimesi>/sointu-studio.git
git push -u origin main
```

Repon pitää olla julkinen, jotta `curl … | sudo bash` toimii.

## Oikeudet

Kouluta vain musiikilla ja äänellä, johon sinulla on oikeudet. Äänimallia saa käyttää vain omasta äänestäsi tai sellaisen henkilön äänestä, joka on antanut siihen luvan.
