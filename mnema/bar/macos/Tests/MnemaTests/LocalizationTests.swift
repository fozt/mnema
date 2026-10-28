import Testing

@testable import Mnema

@Test func languageTagsNormalizeToRussianOrEnglish() {
    #expect(UILanguage.normalize("ru") == "ru")
    #expect(UILanguage.normalize("ru-RU") == "ru")
    #expect(UILanguage.normalize("RU_ru") == "ru")
    #expect(UILanguage.normalize("en-GB") == "en")
    #expect(UILanguage.normalize("uk") == "en")
    #expect(UILanguage.normalize("") == "en")
    #expect(UILanguage.normalize(nil) == "en")
}

@Test func russianComesFromTheCatalogWithEnglishFallback() {
    let catalog = ["Recordings": "Записи"]
    #expect(Localization.text("Recordings", lang: "ru", catalog: catalog) == "Записи")
    #expect(Localization.text("Recordings", lang: "en", catalog: catalog) == "Recordings")
    #expect(Localization.text("Quit Mnema", lang: "ru", catalog: catalog) == "Quit Mnema")
}

@Test func templatesFillPlaceholdersInOnePass() {
    #expect(Localization.fill("Поиск в разделе «{name}»", ["name": "Projects"]) == "Поиск в разделе «Projects»")
    #expect(Localization.fill("{a} and {b}", ["a": "{b}", "b": "x"]) == "{b} and x")
    #expect(Localization.fill("{missing} {", [:]) == "{missing} {")
    #expect(Localization.fill("{n}{n}", ["n": "1"]) == "11")
}
