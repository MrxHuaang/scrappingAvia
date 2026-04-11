/**
 * AIRCRAFT_DATA - Base de datos de fabricantes y modelos de aeronaves
 * Nombres exactos como aparecen en ADs de FAA, EASA, Transport Canada y ANAC.
 *
 * Estructura por entrada:
 *   aliases : strings alternativos por los que el usuario puede buscar
 *   models  : modelos exactos del fabricante tal como aparecen en los ADs
 */

const AIRCRAFT_DATA = {

    // ---------------------------------------------------------------
    // AVIONES COMERCIALES GRANDES
    // ---------------------------------------------------------------

    'Airbus': {
        aliases: ['airbus', 'a320', 'a330', 'a340', 'a350', 'a380', 'a220', 'a300', 'a310', 'neo', 'ceo'],
        commercial_names: ['A320 Family', 'A320neo', 'A321neo', 'A330neo', 'A350 XWB', 'A380'],
        models: [
            'A220-100', 'A220-300',
            'A300 B2', 'A300 B4', 'A300-600', 'A300-600R',
            'A310-200', 'A310-300',
            'A318-100', 'A319-100', 'A319neo', 'A320-200', 'A320neo',
            'A321-100', 'A321-200', 'A321neo', 'A321XLR',
            'A330-200', 'A330-200F', 'A330-300', 'A330-800neo', 'A330-900neo',
            'A340-200', 'A340-300', 'A340-500', 'A340-600',
            'A350-900', 'A350-900ULR', 'A350-1000',
            'A380-800',
        ],
    },

    'Boeing': {
        aliases: ['boeing', '737', '747', '757', '767', '777', '787', '717', '727', '707', 'max', 'dreamliner', 'mcdonnell douglas'],
        commercial_names: ['737 MAX', '737 Classic', '737 NG', '747 Jumbo', '777 Triple Seven', '787 Dreamliner', '767', '757'],
        models: [
            '707-100', '707-200', '707-300', '707-400',
            '717-200',
            '727-100', '727-200',
            '737-100', '737-200', '737-300', '737-400', '737-500',
            '737-600', '737-700', '737-800', '737-900', '737-900ER',
            '737 MAX 7', '737 MAX 8', '737 MAX 9', '737 MAX 10',
            '747-100', '747-200', '747-300', '747-400', '747-400F',
            '747-8', '747-8F',
            '757-200', '757-200PF', '757-300',
            '767-200', '767-200ER', '767-300', '767-300ER', '767-400ER', '767-300F',
            '777-200', '777-200ER', '777-200LR', '777-300', '777-300ER', '777F',
            '777X', '777-8', '777-9',
            '787-8', '787-9', '787-10',
        ],
    },

    'McDonnell Douglas': {
        aliases: ['mcdonnell douglas', 'mdd', 'dc-9', 'dc-10', 'md-11', 'md-80', 'md-88', 'md-90', 'dc9', 'dc10'],
        models: [
            'DC-9-10', 'DC-9-20', 'DC-9-30', 'DC-9-40', 'DC-9-50',
            'DC-10-10', 'DC-10-15', 'DC-10-30', 'DC-10-40',
            'MD-11', 'MD-11F',
            'MD-81', 'MD-82', 'MD-83', 'MD-87', 'MD-88', 'MD-90-30',
        ],
    },

    // ---------------------------------------------------------------
    // AVIONES REGIONALES
    // ---------------------------------------------------------------

    'Bombardier': {
        aliases: [
            'bombardier', 'canadair', 'crj', 'crj200', 'crj700', 'crj900', 'crj1000',
            'dash 8', 'q400', 'q300', 'q200', 'q100', 'dhc', 'learjet', 'challenger',
            'global', 'global express', 'global 5000', 'global 6000', 'cl-600',
        ],
        commercial_names: ['CRJ', 'Dash 8', 'Q Series', 'Global', 'Challenger', 'Learjet'],
        models: [
            // CRJ Series
            'CRJ-100', 'CRJ-200', 'CRJ-440', 'CRJ-700', 'CRJ-705', 'CRJ-900', 'CRJ-1000',
            // Q Series (Dash 8)
            'DHC-8-100', 'DHC-8-200', 'DHC-8-300', 'DHC-8-400', 'DHC-8Q-400',
            // Challenger
            'CL-600-1A11', 'CL-600-2A12', 'CL-600-2B16', 'CL-600-2B19',
            'Challenger 300', 'Challenger 350', 'Challenger 604', 'Challenger 605', 'Challenger 650',
            // Global
            'Global Express', 'Global 5000', 'Global 6000', 'Global 7500',
            // Learjet
            'Learjet 45', 'Learjet 55', 'Learjet 60', 'Learjet 70', 'Learjet 75',
        ],
    },

    'Embraer': {
        aliases: ['embraer', 'erj', 'emb', 'erj-145', 'erj-135', 'erj-140', 'e-jet', 'e175', 'e190', 'e195', 'phenom', 'legacy', 'praetor', 'lineage'],
        commercial_names: ['E-Jet', 'E170', 'E175', 'E190', 'E195', 'ERJ', 'Phenom', 'Legacy'],
        models: [
            'EMB-120', 'EMB-120ER', 'EMB-120RT',
            'ERJ-135BJ', 'ERJ-135ER', 'ERJ-135LR',
            'ERJ-140LR',
            'ERJ-145ER', 'ERJ-145LR', 'ERJ-145MP', 'ERJ-145EP', 'ERJ-145XR',
            'EMB-170-100', 'EMB-170-200',
            'EMB-190-100', 'EMB-190-200',
            'EMB-190E2', 'EMB-195E2',
            'Phenom 100', 'Phenom 100E', 'Phenom 300', 'Phenom 300E',
            'Legacy 450', 'Legacy 500', 'Legacy 600', 'Legacy 650',
            'Praetor 500', 'Praetor 600',
            'Lineage 1000E',
        ],
    },

    'ATR': {
        aliases: ['atr', 'atr-42', 'atr-72', 'avions de transport regional'],
        models: [
            'ATR 42-200', 'ATR 42-300', 'ATR 42-320', 'ATR 42-500', 'ATR 42-600',
            'ATR 72-100', 'ATR 72-200', 'ATR 72-210', 'ATR 72-500', 'ATR 72-600',
            'ATR 72-212A',
        ],
    },

    'Saab': {
        aliases: ['saab', 'saab 340', 'saab 2000', 'fairchild-swearingen'],
        models: [
            'SAAB 340A', 'SAAB 340B', 'SAAB 340B Plus',
            'SAAB 2000',
            'SF340A', 'SF340B',
        ],
    },

    'Fokker': {
        aliases: ['fokker', 'f27', 'f28', 'f50', 'f70', 'f100', 'friendship', 'fellowship'],
        models: [
            'F27 Friendship', 'F27 Mark 100', 'F27 Mark 200', 'F27 Mark 500', 'F27 Mark 600',
            'F28 Fellowship', 'F28 Mark 1000', 'F28 Mark 4000',
            'F50', 'F60',
            'F70', 'F100',
        ],
    },

    // ---------------------------------------------------------------
    // AVIONES DE AVIACION GENERAL - MOTOR PISTON
    // ---------------------------------------------------------------

    'Cessna': {
        aliases: ['cessna', 'cessna aircraft', '172', '182', '206', '208', 'skyhawk', 'skylane', 'caravan', 'citation', 'stationair', 'textron cessna'],
        commercial_names: ['Caravan', 'Citation', 'Skyhawk', 'Skylane', 'Stationair'],
        models: [
            '150', '152',
            '172', '172N', '172P', '172R', '172S', '172SP',
            '177', '177B', '177RG',
            '182', '182P', '182Q', '182R', '182S', '182T',
            '185', '185F',
            '206', '206H', 'T206H', 'U206G',
            '207', 'T207A',
            '208', '208A', '208B',
            '210', '210N', '210R', 'T210N', 'T210R',
            '310', '320', '337',
            '340', '402', '404', '414', '421',
            // Citation
            'Citation 500', 'Citation I', 'Citation II', 'Citation III',
            'Citation V', 'Citation VI', 'Citation VII',
            '525', '525A', '525B', '525C',
            '550', '551', '560', '560XL', '560XLS',
            '650', '680', '680A', '680B',
            '700', '750',
        ],
    },

    'Piper': {
        aliases: ['piper', 'piper aircraft', 'pa-28', 'pa-32', 'pa-34', 'pa-44', 'pa-46', 'cherokee', 'arrow', 'seneca', 'seminole', 'malibu', 'meridian', 'matrix'],
        models: [
            'PA-18', 'PA-18-150',
            'PA-22', 'PA-22-150', 'PA-22-160',
            'PA-23', 'PA-23-160', 'PA-23-250',
            'PA-24-260',
            'PA-28-140', 'PA-28-150', 'PA-28-151', 'PA-28-160', 'PA-28-161',
            'PA-28-180', 'PA-28-181', 'PA-28-235', 'PA-28-236',
            'PA-28R-180', 'PA-28R-200', 'PA-28R-201', 'PA-28R-201T',
            'PA-30', 'PA-30-160',
            'PA-31', 'PA-31-350', 'PA-31P',
            'PA-31T', 'PA-31T1', 'PA-31T2', 'PA-31T3',
            'PA-32-260', 'PA-32-300', 'PA-32-301', 'PA-32-301T',
            'PA-32R-300', 'PA-32R-301', 'PA-32R-301T',
            'PA-34-200', 'PA-34-200T', 'PA-34-220T',
            'PA-38-112',
            'PA-39', 'PA-40',
            'PA-44-180', 'PA-44-180T',
            'PA-46-310P', 'PA-46-350P', 'PA-46-500TP', 'PA-46-600TP', 'PA-46R-350T',
        ],
    },

    'Beechcraft': {
        aliases: ['beechcraft', 'beech', 'raytheon', 'hawker beechcraft', 'bonanza', 'baron', 'king air', 'super king air', '1900', 'starship', 'musketeer'],
        models: [
            'A23', 'B23', 'C23',
            'A36', 'B36TC', 'G36',
            'F33A', 'F33C',
            'E55', 'P58',
            'A58', 'B58',
            '76', 'Duchess',
            'A90', 'B90', 'C90', 'C90A', 'C90B', 'C90GT', 'C90GTi',
            'E90', 'F90', 'F90-1',
            'H90',
            '99', '99A', 'B99', 'C99',
            'A100', 'B100',
            '200', '200C', '200CT', 'B200', 'B200C', 'B200GT',
            '300', '300LW', 'B300', 'B300C',
            '350', '350C', '350ER', '350i',
            '1900', '1900C', '1900D',
            'King Air 90', 'King Air 100', 'King Air 200', 'King Air 250', 'King Air 300', 'King Air 350',
        ],
    },

    'Mooney': {
        aliases: ['mooney', 'm20', 'acclaim', 'ovation', 'bravo', 'eagle'],
        models: [
            'M20', 'M20A', 'M20B', 'M20C', 'M20D', 'M20E', 'M20F', 'M20G',
            'M20J', 'M20K', 'M20L', 'M20M', 'M20R', 'M20S', 'M20TN', 'M20U', 'M20V',
        ],
    },

    'Cirrus Aircraft': {
        aliases: ['cirrus', 'sr20', 'sr22', 'sf50', 'vision jet', 'cirrus design'],
        models: [
            'SR20', 'SR20-G1', 'SR20-G2', 'SR20-G3', 'SR20-G6',
            'SR22', 'SR22-G1', 'SR22-G2', 'SR22-G3', 'SR22-G5', 'SR22-G6',
            'SR22T', 'SR22T-G5', 'SR22T-G6',
            'SF50', 'SF50-G1', 'SF50-G2',
        ],
    },

    'Diamond Aircraft': {
        aliases: ['diamond', 'diamond aircraft', 'da40', 'da42', 'da62', 'katana', 'da20', 'da36'],
        models: [
            'DA20-A1', 'DA20-C1',
            'DA40', 'DA40-180', 'DA40 NG', 'DA40-D', 'DA40 TDI', 'DA40 XLS', 'DA40-CS',
            'DA42', 'DA42 NG', 'DA42 M-NG', 'DA42-VI',
            'DA62',
        ],
    },

    'Maule': {
        aliases: ['maule', 'm-5', 'm-6', 'm-7', 'm-8', 'm-9', 'mt-7'],
        models: [
            'M-4', 'M-4-180C',
            'M-5-180C', 'M-5-210C', 'M-5-220C', 'M-5-235C',
            'M-6-180', 'M-6-235',
            'MX-7-180A', 'MX-7-180C', 'MX-7-235',
            'M-7-180', 'M-7-235',
            'M-8-235',
            'M-9-235',
            'MT-7-180', 'MT-7-235',
        ],
    },

    'American Champion': {
        aliases: ['american champion', 'bellanca', 'citabria', 'scout', 'decathlon', '7gcbc', '8gcbc'],
        models: [
            '7EC', '7FC',
            '7GCAA', '7GCBC',
            '7KCAB',
            '8GCBC',
            '8KCAB',
            'Scout', 'Super Scout',
            'Citabria', 'Aurora',
            'Decathlon',
        ],
    },

    // ---------------------------------------------------------------
    // JETS EJECUTIVOS
    // ---------------------------------------------------------------

    'Gulfstream': {
        aliases: ['gulfstream', 'grumman', 'g100', 'g150', 'g200', 'g280', 'g350', 'g400', 'g450', 'g500', 'g550', 'g600', 'g650', 'g700', 'g800'],
        models: [
            'Gulfstream G100', 'Gulfstream G150',
            'Gulfstream G200', 'Gulfstream G280',
            'Gulfstream GIV', 'Gulfstream GIV-X', 'Gulfstream G350', 'Gulfstream G400', 'Gulfstream G450',
            'Gulfstream GV', 'Gulfstream GV-SP', 'Gulfstream G500', 'Gulfstream G550',
            'Gulfstream G600',
            'Gulfstream G650', 'Gulfstream G650ER',
            'Gulfstream G700', 'Gulfstream G800',
        ],
    },

    'Dassault Aviation': {
        aliases: ['dassault', 'falcon', 'falcon 900', 'falcon 2000', 'falcon 7x', 'falcon 8x', 'mystere', 'falcon 50'],
        models: [
            'Falcon 10', 'Falcon 20', 'Falcon 50', 'Falcon 50EX',
            'Falcon 900', 'Falcon 900B', 'Falcon 900C', 'Falcon 900DX', 'Falcon 900EX', 'Falcon 900EX EASy', 'Falcon 900LX',
            'Falcon 2000', 'Falcon 2000EX', 'Falcon 2000EX EASy', 'Falcon 2000LX', 'Falcon 2000LXS', 'Falcon 2000S',
            'Falcon 7X',
            'Falcon 8X',
            'Falcon 6X',
        ],
    },

    'Pilatus': {
        aliases: ['pilatus', 'pc-6', 'pc-7', 'pc-9', 'pc-12', 'pc-21', 'pc-24', 'porter', 'turboporter'],
        models: [
            'PC-6', 'PC-6/B1-H2', 'PC-6/B2-H2', 'PC-6/B2-H4',
            'PC-7', 'PC-7 Mk II',
            'PC-9', 'PC-9 M',
            'PC-12', 'PC-12/45', 'PC-12/47', 'PC-12/47E', 'PC-12 NGX',
            'PC-21',
            'PC-24',
        ],
    },

    // ---------------------------------------------------------------
    // HELICOPTEROS
    // ---------------------------------------------------------------

    'Bell Helicopter': {
        aliases: ['bell', 'bell helicopter', 'bell textron', '206', '212', '214', '222', '230', '407', '412', '429', '430', '505', '525', '412ep', '412epi'],
        models: [
            'Bell 47', 'Bell 47G',
            'Bell 206A', 'Bell 206B', 'Bell 206B-3', 'Bell 206L', 'Bell 206L-1', 'Bell 206L-3', 'Bell 206L-4',
            'Bell 212', 'Bell 214B', 'Bell 214B-1',
            'Bell 222', 'Bell 222B', 'Bell 222U',
            'Bell 230', 'Bell 249',
            'Bell 407', 'Bell 407GX', 'Bell 407GXi', 'Bell 407GXP',
            'Bell 412', 'Bell 412EP', 'Bell 412EPI', 'Bell 412HP',
            'Bell 427',
            'Bell 429', 'Bell 429WLG',
            'Bell 430',
            'Bell 505',
            'Bell 525',
        ],
    },

    'Sikorsky': {
        aliases: ['sikorsky', 's-61', 's-70', 's-76', 's-92', 'uh-60', 'blackhawk', 'seahawk', 's76', 's92'],
        models: [
            'S-61A', 'S-61B', 'S-61L', 'S-61N', 'S-61NM', 'S-61R',
            'S-70', 'S-70A', 'S-70B', 'S-70C',
            'S-76', 'S-76A', 'S-76A+', 'S-76B', 'S-76C', 'S-76C+', 'S-76C++', 'S-76D',
            'S-92', 'S-92A',
            'UH-60A', 'UH-60L', 'UH-60M',
        ],
    },

    'Leonardo': {
        aliases: ['leonardo', 'agustawestland', 'agusta', 'aw109', 'aw119', 'aw139', 'aw149', 'aw169', 'aw189', 'aw101', 'a109', 'a119', 'a139', 'finmeccanica'],
        models: [
            'A109', 'A109A', 'A109A II', 'A109C', 'A109E', 'A109K2', 'A109S',
            'AW109SP', 'AW109E',
            'A119 Koala', 'AW119 Mk II', 'AW119Kx',
            'A129', 'AW129',
            'AB139',
            'AW139',
            'AW149',
            'AW169',
            'AW189',
            'AW101',
        ],
    },

    'Eurocopter': {
        aliases: ['eurocopter', 'aerospatiale', 'as350', 'as355', 'as332', 'ec120', 'ec130', 'ec135', 'ec145', 'ec155', 'ec175', 'ecureuil', 'squirrel', 'dauphin', 'super puma'],
        models: [
            'AS350B', 'AS350B1', 'AS350B2', 'AS350B3', 'AS350BA', 'AS350C', 'AS350D',
            'AS355E', 'AS355F', 'AS355F1', 'AS355F2', 'AS355N', 'AS355NP',
            'AS332C', 'AS332C1', 'AS332L', 'AS332L1', 'AS332L2',
            'AS365C', 'AS365C2', 'AS365C3', 'AS365N', 'AS365N1', 'AS365N2', 'AS365N3',
            'EC120B',
            'EC130B4', 'EC130T2',
            'EC135P1', 'EC135P2', 'EC135P2+', 'EC135P3', 'EC135T1', 'EC135T2', 'EC135T2+', 'EC135T3',
            'EC145', 'EC145T2',
            'EC155B', 'EC155B1',
            'EC175B',
        ],
    },

    'Airbus Helicopters': {
        aliases: ['airbus helicopters', 'airbus helicopter', 'h125', 'h130', 'h135', 'h145', 'h155', 'h160', 'h175', 'h215', 'h225', 'super puma mk2', 'ec225'],
        models: [
            'H120', 'H125', 'H130',
            'H135', 'H135 M', 'H135 P',
            'H145', 'H145M', 'H145D2', 'H145D3',
            'H155B', 'H155B1',
            'H160',
            'H175', 'H175M',
            'H215', 'H215M',
            'H225', 'H225M',
        ],
    },

    'Robinson Helicopter': {
        aliases: ['robinson', 'r22', 'r44', 'r66', 'robinson helicopter'],
        models: [
            'R22', 'R22 Alpha', 'R22 Beta', 'R22 Beta II', 'R22 Mariner', 'R22IFR',
            'R44', 'R44 Astro', 'R44 Clipper', 'R44 II', 'R44 Cadet', 'R44 Clipper II', 'R44 Newscopter', 'R44 Police',
            'R66', 'R66 Turbine', 'R66 Turbine Marine',
        ],
    },

    'MD Helicopters': {
        aliases: ['md helicopters', 'mdhc', 'md500', 'md520', 'md530', 'md600', 'md902', 'hughes', 'hughes helicopters'],
        models: [
            'MD 500C', 'MD 500D', 'MD 500E', 'MD 500MG', 'MD 500MD',
            'MD 520N',
            'MD 530F', 'MD 530G', 'MD 530FF',
            'MD 600N',
            'MD Explorer', 'MD 900', 'MD 902',
            '369D', '369E', '369FF', '369H', '369HE', '369HM', '369HS',
        ],
    },

    'Schweizer': {
        aliases: ['schweizer', '269', '300', '330', '333', 'hughes 300'],
        models: [
            'Hughes 269A', 'Hughes 269B', 'Hughes 269C',
            '300C', '300CB', '300CBi',
            '330SP',
            '333',
            'TH300C',
        ],
    },

    // ---------------------------------------------------------------
    // FABRICANTES ESPECIALIZADOS / AVIONES DE NICHO
    // ---------------------------------------------------------------

    'de Havilland Canada': {
        aliases: ['de havilland canada', 'dhc', 'dhc-2', 'dhc-3', 'dhc-6', 'dhc-7', 'dhc-8', 'beaver', 'otter', 'twin otter', 'dash 7', 'dash 8', 'viking air'],
        models: [
            'DHC-1', 'DHC-1B',
            'DHC-2', 'DHC-2 Mk.I', 'DHC-2 Mk.III',
            'DHC-3', 'DHC-3T',
            'DHC-4', 'DHC-4A',
            'DHC-5', 'DHC-5A', 'DHC-5D',
            'DHC-6-100', 'DHC-6-200', 'DHC-6-300', 'DHC-6-400',
            'DHC-7-100', 'DHC-7-102', 'DHC-7-150',
            'DHC-8-100', 'DHC-8-102', 'DHC-8-200', 'DHC-8-300', 'DHC-8-400',
        ],
    },

    'Viking Air': {
        aliases: ['viking air', 'viking', 'dhc-6 twin otter', 'series 400'],
        models: [
            'DHC-6-400 Twin Otter',
            'DHC-6-400 Series',
        ],
    },

    'Lockheed': {
        aliases: ['lockheed', 'lockheed martin', 'l-1011', 'c-130', 'hercules', 'p-3', 'orion', 'tristar'],
        models: [
            'L-100-20', 'L-100-30',
            'L-188', 'L-188A', 'L-188C',
            'L-1011-1', 'L-1011-100', 'L-1011-200', 'L-1011-500',
            'C-130A', 'C-130B', 'C-130E', 'C-130H', 'C-130J',
            'P-3A', 'P-3B', 'P-3C',
        ],
    },

    'Textron Aviation': {
        aliases: ['textron', 'textron aviation', 'beechcraft textron', 'cessna textron'],
        models: [
            // Hereda Cessna y Beechcraft
            'Cessna 172', 'Cessna 182', 'Cessna 208', 'Cessna Citation CJ3+',
            'Beechcraft King Air 350', 'Beechcraft Bonanza G36', 'Beechcraft Baron G58',
        ],
    },

    'Raytheon Aircraft': {
        aliases: ['raytheon', 'raytheon aircraft', 'hawker', 'hawker beechcraft', 'hawker 400', 'hawker 800', 'hawker 900', 'hawker 4000'],
        models: [
            'Hawker 400', 'Hawker 400A', 'Hawker 400XP',
            'Hawker 700', 'Hawker 700A',
            'Hawker 750',
            'Hawker 800', 'Hawker 800A', 'Hawker 800XP', 'Hawker 850XP',
            'Hawker 900XP',
            'Hawker 1000', 'Hawker 4000',
            'Beechcraft 350', 'Beechcraft B200',
        ],
    },

    'Socata': {
        aliases: ['socata', 'socata aircraft', 'tb-9', 'tb-10', 'tb-20', 'tb-21', 'tampico', 'tobago', 'trinidad', 'tbm', 'tbm 700', 'tbm 850', 'tbm 900'],
        models: [
            'TB 9', 'TB 10', 'TB 20', 'TB 21',
            'TBM 700', 'TBM 700A', 'TBM 700B', 'TBM 700C', 'TBM 700C1', 'TBM 700C2',
            'TBM 850',
        ],
    },

    'DAHER': {
        aliases: ['daher', 'daher-socata', 'tbm', 'tbm 900', 'tbm 910', 'tbm 930', 'tbm 940', 'tbm 960'],
        models: [
            'TBM 900', 'TBM 910', 'TBM 930', 'TBM 940', 'TBM 960',
        ],
    },

    'Tecnam': {
        aliases: ['tecnam', 'p2002', 'p2006t', 'p2010', 'p2012', 'p92', 'p-92', 'p-2002'],
        models: [
            'P92-J', 'P92-S', 'P92-S Echo', 'P92-RG',
            'P2002-JF', 'P2002-JR', 'P2002-RG', 'P2002-Sierra',
            'P2004 Bravo',
            'P2006T', 'P2006T MMA',
            'P2010', 'P2010 TDI',
            'P2012 Traveller',
        ],
    },

    'Grob Aircraft': {
        aliases: ['grob', 'grob aircraft', 'g115', 'g120', 'g140'],
        models: [
            'G 102', 'G 103', 'G 103A', 'G 103C',
            'G 109', 'G 109A', 'G 109B',
            'G 115', 'G 115A', 'G 115B', 'G 115C', 'G 115D', 'G 115E', 'G 115T',
            'G 120A', 'G 120TP',
            'G 140',
        ],
    },

    'Extra Aircraft': {
        aliases: ['extra', 'extra aircraft', 'ea-300', 'ea-330', 'ea-400'],
        models: [
            'EA-200',
            'EA-300', 'EA-300L', 'EA-300S', 'EA-300/200', 'EA-300/SC',
            'EA-330LX', 'EA-330SC',
            'EA-400',
        ],
    },

    'Schempp-Hirth': {
        aliases: ['schempp-hirth', 'schempp hirth', 'discus', 'nimbus', 'ventus', 'arcus', 'duo discus'],
        models: [
            'Discus a', 'Discus b', 'Discus bT', 'Discus-2a', 'Discus-2b', 'Discus-2c',
            'Discus-2cT', 'Discus-2cFES',
            'Duo Discus', 'Duo Discus T', 'Duo Discus xT',
            'Nimbus-3', 'Nimbus-3D', 'Nimbus-4', 'Nimbus-4D', 'Nimbus-4DT',
            'Ventus a', 'Ventus b', 'Ventus bT', 'Ventus-2a', 'Ventus-2b', 'Ventus-2cT',
            'Arcus', 'Arcus E', 'Arcus M', 'Arcus T',
        ],
    },

    'Stemme': {
        aliases: ['stemme', 's10', 's12'],
        models: [
            'S 10', 'S 10-V', 'S 10-VT', 'S 10-VX',
            'S 12',
        ],
    },

    'Pipistrel': {
        aliases: ['pipistrel', 'alpha', 'sinus', 'virus', 'taurus', 'panthera', 'velis'],
        models: [
            'Alpha Trainer', 'Alpha Electro',
            'Sinus 912', 'Sinus 914', 'Sinus Flex',
            'Virus SW 80', 'Virus SW 100', 'Virus SW 121', 'Virus SW 128', 'Virus 912',
            'Taurus', 'Taurus Electro', 'Taurus G4',
            'Panthera',
            'Velis Electro',
        ],
    },

    'Flight Design': {
        aliases: ['flight design', 'ctsw', 'ctls', 'ct2k', 'ctsl', 'mc'],
        models: [
            'CT2K',
            'CTSW',
            'CTLS',
            'CTSuper',
            'MC',
            'F2',
        ],
    },

    // ---------------------------------------------------------------
    // MOTORES
    // ---------------------------------------------------------------

    'Pratt & Whitney Canada': {
        aliases: ['pratt whitney canada', 'pwc', 'pt6', 'pt6a', 'pw100', 'pw150', 'pw600', 'jt15d', 'pw500', 'pratt and whitney canada'],
        models: [
            'PT6A-6', 'PT6A-20', 'PT6A-21', 'PT6A-25', 'PT6A-27', 'PT6A-28',
            'PT6A-34', 'PT6A-34B', 'PT6A-36', 'PT6A-38', 'PT6A-41', 'PT6A-42',
            'PT6A-45R', 'PT6A-60A', 'PT6A-62', 'PT6A-65', 'PT6A-66', 'PT6A-67',
            'PT6A-114', 'PT6A-114A', 'PT6A-135', 'PT6A-135A',
            'PT6T-3', 'PT6T-3B', 'PT6T-3BF', 'PT6T-3D', 'PT6T-3DF',
            'JT15D-1', 'JT15D-1A', 'JT15D-1B', 'JT15D-4', 'JT15D-5',
            'PW100', 'PW118', 'PW118A', 'PW118B', 'PW119B', 'PW120', 'PW121', 'PW123',
            'PW150A',
            'PW500', 'PW530A', 'PW535A', 'PW545A',
            'PW600', 'PW610F', 'PW615F', 'PW617F',
        ],
    },

    'CFM International': {
        aliases: ['cfm', 'cfm international', 'cfm56', 'leap', 'leap-1a', 'leap-1b', 'leap-1c'],
        models: [
            'CFM56-2', 'CFM56-2A', 'CFM56-2B', 'CFM56-2C',
            'CFM56-3', 'CFM56-3B', 'CFM56-3C',
            'CFM56-5', 'CFM56-5A', 'CFM56-5B', 'CFM56-5C',
            'CFM56-7', 'CFM56-7B',
            'LEAP-1A', 'LEAP-1B', 'LEAP-1C',
        ],
    },

    'General Electric Aviation': {
        aliases: ['general electric', 'ge', 'ge aviation', 'cf6', 'ge90', 'genx', 'cf34', 'ct7', 'passport'],
        models: [
            'CF6-6', 'CF6-50', 'CF6-50A', 'CF6-50C', 'CF6-80', 'CF6-80A', 'CF6-80C2', 'CF6-80E1',
            'GE90-76B', 'GE90-85B', 'GE90-90B', 'GE90-94B', 'GE90-110B1', 'GE90-115B',
            'GEnx-1B', 'GEnx-2B',
            'CF34-1A', 'CF34-3A', 'CF34-3B', 'CF34-8C', 'CF34-8E', 'CF34-10E',
            'CT7-2A', 'CT7-2D', 'CT7-5A', 'CT7-6', 'CT7-9B', 'CT7-9C', 'CT7-9D',
            'Passport 20',
        ],
    },

    'Rolls-Royce': {
        aliases: ['rolls-royce', 'rolls royce', 'rr', 'rb211', 'trent', 'ae 3007', 'br700', 'spey', 'dart'],
        models: [
            'Dart 526', 'Dart 528', 'Dart 532', 'Dart 536',
            'Spey 506', 'Spey 511', 'Spey 512',
            'RB211-22B', 'RB211-524', 'RB211-524B', 'RB211-524C', 'RB211-524D4', 'RB211-524G', 'RB211-524H',
            'RB211-535C', 'RB211-535E4', 'RB211-535E4B',
            'Trent 553', 'Trent 556', 'Trent 560', 'Trent 768', 'Trent 772', 'Trent 772B',
            'Trent 800', 'Trent 892', 'Trent 895',
            'Trent 900', 'Trent 970', 'Trent 972',
            'Trent 1000', 'Trent 1000-A', 'Trent 1000-C', 'Trent 1000-G', 'Trent 1000-H',
            'Trent XWB-84', 'Trent XWB-97',
            'AE 3007A', 'AE 3007A1', 'AE 3007A1E', 'AE 3007C',
            'BR710A1-10', 'BR715', 'BR725',
        ],
    },

    'Honeywell': {
        aliases: ['honeywell', 'allied signal', 'alliedsignal', 'garrett', 'tfe731', 'alf502', 'lf507', 'htf7000', 'as907', 'tpe331'],
        models: [
            'TPE331-1', 'TPE331-2', 'TPE331-3', 'TPE331-5', 'TPE331-6', 'TPE331-10', 'TPE331-11', 'TPE331-12',
            'TFE731-2', 'TFE731-3', 'TFE731-4', 'TFE731-5', 'TFE731-20', 'TFE731-40', 'TFE731-60',
            'ALF502L', 'ALF502R', 'ALF502R-3', 'ALF502R-5',
            'LF507-1F',
            'HTF7000',
            'AS907-1-1A',
        ],
    },

    'Lycoming': {
        aliases: ['lycoming', 'avco lycoming', 'textron lycoming', 'io-360', 'io-540', 'o-360', 'o-540', 'tio-540'],
        models: [
            'O-235', 'O-235-C1', 'O-235-L2C',
            'O-320', 'O-320-A', 'O-320-B', 'O-320-D', 'O-320-E', 'O-320-H2AD',
            'O-360', 'O-360-A', 'O-360-C', 'O-360-E',
            'IO-360', 'IO-360-A', 'IO-360-B', 'IO-360-C', 'IO-360-L2A', 'IO-360-M1A',
            'IO-390', 'IO-390-A3B6',
            'O-540', 'O-540-A', 'O-540-B', 'O-540-E', 'O-540-J',
            'IO-540', 'IO-540-A', 'IO-540-C', 'IO-540-D', 'IO-540-K', 'IO-540-M', 'IO-540-W',
            'IO-580', 'IO-580-B1A',
            'TIO-540', 'TIO-540-A', 'TIO-540-AF1B', 'TIO-540-AH1A', 'TIO-540-J2BD',
            'AEIO-360', 'AEIO-360-A', 'AEIO-360-B',
            'AEIO-540', 'AEIO-540-D', 'AEIO-540-L1B5',
        ],
    },

    'Continental Motors': {
        aliases: ['continental', 'continental motors', 'teledyne continental', 'io-360 continental', 'io-520', 'io-550', 'tsio-520', 'o-200', 'o-300'],
        models: [
            'A65', 'A75', 'A80',
            'C75', 'C85', 'C90',
            'O-200-A', 'O-200-B', 'O-200-D',
            'O-300', 'O-300-A', 'O-300-B', 'O-300-C', 'O-300-D',
            'IO-240-A', 'IO-240-B',
            'O-470', 'O-470-G', 'O-470-K', 'O-470-L', 'O-470-R', 'O-470-S', 'O-470-U',
            'IO-360', 'IO-360-A', 'IO-360-C', 'IO-360-CB', 'IO-360-D', 'IO-360-DB', 'IO-360-ES', 'IO-360-GB',
            'IO-470', 'IO-470-C', 'IO-470-D', 'IO-470-E', 'IO-470-F', 'IO-470-G', 'IO-470-K', 'IO-470-L', 'IO-470-M',
            'IO-520', 'IO-520-A', 'IO-520-B', 'IO-520-BA', 'IO-520-BB', 'IO-520-C', 'IO-520-CB', 'IO-520-D', 'IO-520-E', 'IO-520-F', 'IO-520-J', 'IO-520-L', 'IO-520-M', 'IO-520-MB', 'IO-520-N', 'IO-520-NB',
            'IO-550', 'IO-550-A', 'IO-550-B', 'IO-550-C', 'IO-550-D', 'IO-550-F', 'IO-550-G', 'IO-550-L', 'IO-550-N', 'IO-550-P', 'IO-550-R',
            'TSIO-360', 'TSIO-360-A', 'TSIO-360-AB', 'TSIO-360-C', 'TSIO-360-D', 'TSIO-360-E', 'TSIO-360-EB', 'TSIO-360-F', 'TSIO-360-FB', 'TSIO-360-GB', 'TSIO-360-HB', 'TSIO-360-JB', 'TSIO-360-KB', 'TSIO-360-LB', 'TSIO-360-MB', 'TSIO-360-RB', 'TSIO-360-SB',
            'TSIO-520', 'TSIO-520-AE', 'TSIO-520-AF', 'TSIO-520-B', 'TSIO-520-BB', 'TSIO-520-BE', 'TSIO-520-C', 'TSIO-520-CE', 'TSIO-520-D', 'TSIO-520-DB', 'TSIO-520-E', 'TSIO-520-EB', 'TSIO-520-G', 'TSIO-520-J', 'TSIO-520-JB', 'TSIO-520-K', 'TSIO-520-KB', 'TSIO-520-L', 'TSIO-520-LB', 'TSIO-520-M', 'TSIO-520-N', 'TSIO-520-NB', 'TSIO-520-R', 'TSIO-520-T', 'TSIO-520-UB', 'TSIO-520-VB', 'TSIO-520-WB',
            'TSIO-550', 'TSIO-550-A', 'TSIO-550-B', 'TSIO-550-C', 'TSIO-550-E', 'TSIO-550-G',
            'LTSIO-520-AE',
        ],
    },

    'Rotax': {
        aliases: ['rotax', 'brs', '912', '914', '915', '916', 'rotax 912', 'rotax 914', 'rotax 915', 'brs rotax'],
        models: [
            'Rotax 912 A', 'Rotax 912 F', 'Rotax 912 S', 'Rotax 912 UL', 'Rotax 912 ULS',
            'Rotax 912 iS', 'Rotax 912 iSc',
            'Rotax 914 F', 'Rotax 914 UL',
            'Rotax 915 iS', 'Rotax 915 iSc',
            'Rotax 916 iS',
        ],
    },

    // ---------------------------------------------------------------
    // HELICES
    // ---------------------------------------------------------------

    'Hartzell Propeller': {
        aliases: ['hartzell', 'hartzell propeller', 'hc-b', 'hc-e', 'hc-f', 'aluminum propeller', 'composite propeller'],
        models: [
            'HC-B3TN-2/T10173', 'HC-B3TN-3D/T10282',
            'HC-B3TN-5/T10288-2',
            'HC-B4TN-5/T10296B-4R',
            'HC-E2YL-2/E9459', 'HC-E3YL-2/E9747',
            'HC-F5N-3/F9489',
            'PHC-J3YF-1RF/F8468-4',
            'SC-M4A-2', 'SC-M5A-2',
        ],
    },

    'McCauley Propeller': {
        aliases: ['mccauley', 'mccauley propeller', '1a', '2a', '3a', 'd3a', 'b3d', '4hfr'],
        models: [
            '1A103/TCM6948',
            '2AF36C34/90AT',
            '3AF32C86/90A', '3AF40C66-ZFL-90A-0',
            '4HFR34C655/86ECA-0',
            'B3D32C405/90DCA-8',
            'D3A32C405/82NDA-6',
            'C152/57NBP-4',
        ],
    },

    'Sensenich': {
        aliases: ['sensenich', 'sensenich propeller', 'w72', 'w76', '76em', '72em'],
        models: [
            'W72CK-0-54', 'W72CK-0-58',
            'W76EMK-2-44', 'W76EMK-2-48', 'W76EMK-2-54',
            '76EMC8S5-0-62',
            '76JM6S10X-0-60',
            'FC-12-2', 'FC-14-2',
        ],
    },

};
