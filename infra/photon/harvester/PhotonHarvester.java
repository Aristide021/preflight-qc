/*
 * PhotonHarvester — offline oracle for the PreFlight QC error taxonomy.
 *
 * Photon's IMPAnalyzer CLI prints human-readable prose ("ERROR-UUID ... is not
 * same as ...") and never emits the IMFErrorLogger.ErrorCodes enum constants.
 * Scraping that text cannot recover the real error codes.
 *
 * This harvester calls the same entry point the CLI does —
 * IMPAnalyzer.analyzeDelivery(Path) — and reads the structured ErrorObject
 * list, which carries the authentic (errorCode, errorLevel, description)
 * triple straight from the validator.
 *
 * Output: one JSON object per error, on stdout. slf4j-simple writes its own
 * logging to stderr, so stdout stays clean JSONL.
 *
 * Usage:
 *   java -cp "/photon/libs/*:/photon/harvester" PhotonHarvester <pkg_dir>...
 */

import com.netflix.imflibrary.IMFErrorLogger;
import com.netflix.imflibrary.app.IMPAnalyzer;
import com.netflix.imflibrary.utils.ErrorLogger;

import java.io.PrintStream;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.List;
import java.util.Map;

public final class PhotonHarvester {

    private static final PrintStream OUT = System.out;

    public static void main(String[] args) {
        if (args.length == 0) {
            System.err.println("usage: PhotonHarvester <imf_package_dir> [more_dirs...]");
            System.err.println("       PhotonHarvester --dump-codes");
            System.exit(2);
        }

        if ("--dump-codes".equals(args[0])) {
            dumpDeclaredEnums();
            OUT.flush();
            return;
        }

        for (String arg : args) {
            Path pkgPath = Paths.get(arg);
            String pkgName = pkgPath.getFileName() == null
                    ? arg
                    : pkgPath.getFileName().toString();

            Map<String, List<ErrorLogger.ErrorObject>> results;
            try {
                results = IMPAnalyzer.analyzeDelivery(pkgPath);
            } catch (Throwable t) {
                // A package Photon cannot open at all is itself a finding —
                // record it rather than dropping the package silently.
                emit(pkgName, "<delivery>", "HARVEST_EXCEPTION", "FATAL",
                        t.getClass().getSimpleName() + ": " + t.getMessage());
                continue;
            }

            if (results.isEmpty()) {
                emit(pkgName, "<delivery>", "NO_ERRORS", "NONE", "");
                continue;
            }

            for (Map.Entry<String, List<ErrorLogger.ErrorObject>> entry : results.entrySet()) {
                String assetName = entry.getKey();
                List<ErrorLogger.ErrorObject> errors = entry.getValue();

                if (errors == null || errors.isEmpty()) {
                    emit(pkgName, assetName, "NO_ERRORS", "NONE", "");
                    continue;
                }

                for (ErrorLogger.ErrorObject error : errors) {
                    emitError(pkgName, assetName, error);
                }
            }
        }

        OUT.flush();
    }

    /**
     * ErrorObject holds raw Enums. Photon overrides toString() on its error
     * enums to return a human label ("IMF CPL Error"), so name() is required to
     * recover the actual constant ("IMF_CPL_ERROR"). getErrorCode() is declared
     * as bare Enum, so codes are NOT confined to IMFErrorLogger.IMFErrors
     * .ErrorCodes — the declaring class is recorded to keep that visible.
     */
    private static void emitError(String pkg, String asset, ErrorLogger.ErrorObject error) {
        Enum<?> code = asEnum(error.getErrorCode());
        Enum<?> level = asEnum(error.getErrorLevel());

        OUT.print("{\"package\":");
        OUT.print(quote(pkg));
        OUT.print(",\"asset\":");
        OUT.print(quote(asset));
        OUT.print(",\"error_code\":");
        OUT.print(quote(code == null ? "UNKNOWN" : code.name()));
        OUT.print(",\"error_code_label\":");
        OUT.print(quote(String.valueOf(error.getErrorCode())));
        OUT.print(",\"error_code_enum\":");
        OUT.print(quote(code == null ? "" : code.getDeclaringClass().getName()));
        OUT.print(",\"error_level\":");
        OUT.print(quote(level == null ? "UNKNOWN" : level.name()));
        OUT.print(",\"error_description\":");
        OUT.print(quote(nullToEmpty(error.getErrorDescription())));
        OUT.println("}");
    }

    private static void emit(String pkg, String asset, String code, String level, String description) {
        OUT.print("{\"package\":");
        OUT.print(quote(pkg));
        OUT.print(",\"asset\":");
        OUT.print(quote(asset));
        OUT.print(",\"error_code\":");
        OUT.print(quote(code));
        OUT.print(",\"error_code_label\":");
        OUT.print(quote(code));
        OUT.print(",\"error_code_enum\":\"\",\"error_level\":");
        OUT.print(quote(level));
        OUT.print(",\"error_description\":");
        OUT.print(quote(nullToEmpty(description)));
        OUT.println("}");
    }

    /** Emit every constant of Photon's declared error enums — the complete taxonomy. */
    private static void dumpDeclaredEnums() {
        for (IMFErrorLogger.IMFErrors.ErrorCodes code : IMFErrorLogger.IMFErrors.ErrorCodes.values()) {
            OUT.print("{\"kind\":\"error_code\",\"name\":");
            OUT.print(quote(code.name()));
            OUT.print(",\"label\":");
            OUT.print(quote(code.toString()));
            OUT.print(",\"enum\":");
            OUT.print(quote(code.getDeclaringClass().getName()));
            OUT.println("}");
        }
        for (IMFErrorLogger.IMFErrors.ErrorLevels level : IMFErrorLogger.IMFErrors.ErrorLevels.values()) {
            OUT.print("{\"kind\":\"error_level\",\"name\":");
            OUT.print(quote(level.name()));
            OUT.print(",\"label\":");
            OUT.print(quote(level.toString()));
            OUT.print(",\"enum\":");
            OUT.print(quote(level.getDeclaringClass().getName()));
            OUT.println("}");
        }
    }

    private static Enum<?> asEnum(Object value) {
        return (value instanceof Enum<?>) ? (Enum<?>) value : null;
    }

    private static String nullToEmpty(String s) {
        return s == null ? "" : s;
    }

    /** Minimal RFC 8259 string escaping — no JSON library on the Photon classpath. */
    private static String quote(String raw) {
        StringBuilder sb = new StringBuilder(raw.length() + 16);
        sb.append('"');
        for (int i = 0; i < raw.length(); i++) {
            char c = raw.charAt(i);
            switch (c) {
                case '"':  sb.append("\\\""); break;
                case '\\': sb.append("\\\\"); break;
                case '\n': sb.append("\\n");  break;
                case '\r': sb.append("\\r");  break;
                case '\t': sb.append("\\t");  break;
                case '\b': sb.append("\\b");  break;
                case '\f': sb.append("\\f");  break;
                default:
                    if (c < 0x20) {
                        sb.append(String.format("\\u%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
            }
        }
        sb.append('"');
        return sb.toString();
    }

    private PhotonHarvester() {}
}
